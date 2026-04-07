"""
Менеджер ордеров — взаимодействие с T-Invest API.

Выставляет рыночные ордера и отслеживает их статус.
При ошибках логирует, но НЕ повторяет попытку автоматически —
решение о повторе принимает вышестоящий компонент.
"""
import logging
import uuid
from typing import Optional, Tuple

from tinkoff.invest import (
    Client,
    OrderDirection,
    OrderType,
    PostOrderResponse,
    Quotation,
    SandboxClient,
)
from tinkoff.invest.utils import quotation_to_decimal

from trading_bot.config import settings
from trading_bot.db import repository
from trading_bot.db.models import Order

logger = logging.getLogger(__name__)


def _quotation_to_float(q: Quotation) -> float:
    """Конвертировать Quotation (units + nano) в float."""
    return float(quotation_to_decimal(q))


class OrderManager:
    """
    Выставляет и отслеживает ордера через T-Invest API.

    Используется из PositionManager — напрямую из стратегии не вызывается.
    """

    def __init__(self, account_id: str, instrument_id: int) -> None:
        self.account_id = account_id
        self.instrument_id = instrument_id

    def place_market_order(
        self,
        figi: str,
        direction: str,  # "buy" / "sell"
        quantity_lots: int,
        signal_id: Optional[int] = None,
    ) -> Tuple[Optional[Order], Optional[str]]:
        """
        Выставить рыночный ордер.

        Возвращает (order_db_record, broker_order_id) при успехе.
        Возвращает (None, error_message) при ошибке.

        Рыночные ордера исполняются немедленно по текущей цене —
        мы не контролируем цену, но гарантируем исполнение.
        """
        # Генерируем уникальный client_order_id для идемпотентности
        # Если запрос дошёл до брокера но ответ потерялся — повторный запрос
        # с тем же client_order_id не создаст дублирующий ордер
        client_order_id = str(uuid.uuid4())

        api_direction = (
            OrderDirection.ORDER_DIRECTION_BUY
            if direction == "buy"
            else OrderDirection.ORDER_DIRECTION_SELL
        )

        # Сохраняем ордер в БД со статусом "new" ДО отправки в API
        # Это гарантирует, что мы не потеряем ордер даже при падении процесса
        db_order = repository.save_order(
            instrument_id=self.instrument_id,
            signal_id=signal_id,
            direction=direction,
            quantity=quantity_lots,
        )

        logger.info(
            f"Выставляем ордер: {direction.upper()} {quantity_lots} лотов, "
            f"figi={figi}, account={self.account_id}"
        )

        try:
            if settings.USE_SANDBOX:
                with SandboxClient(settings.TINKOFF_TOKEN) as client:
                    response: PostOrderResponse = client.sandbox.post_sandbox_order(
                        figi=figi,
                        quantity=quantity_lots,
                        direction=api_direction,
                        account_id=self.account_id,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                        order_id=client_order_id,
                    )
            else:
                with Client(settings.TINKOFF_TOKEN) as client:
                    response: PostOrderResponse = client.orders.post_order(
                        figi=figi,
                        quantity=quantity_lots,
                        direction=api_direction,
                        account_id=self.account_id,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                        order_id=client_order_id,
                    )

            broker_order_id = response.order_id
            executed_price = _quotation_to_float(response.executed_order_price) if response.executed_order_price else None
            initial_commission = _quotation_to_float(response.initial_commission) if response.initial_commission else None

            # Определяем статус: market order обычно сразу filled
            status_map = {
                1: "new",
                2: "pending",
                3: "cancelled",
                4: "filled",
                5: "rejected",
            }
            status = status_map.get(response.execution_report_status, "pending")

            repository.update_order_status(
                order_id=db_order.id,
                status=status,
                price_executed=executed_price,
                commission_rub=initial_commission,
                order_id_broker=broker_order_id,
            )

            logger.info(
                f"Ордер принят брокером: id={broker_order_id}, "
                f"status={status}, цена={executed_price}"
            )

            # Обновляем объект из БД
            db_order.order_id_broker = broker_order_id
            db_order.status = status
            db_order.price_executed = executed_price
            db_order.commission_rub = initial_commission

            return db_order, None

        except Exception as e:
            error_msg = f"Ошибка выставления ордера: {e}"
            logger.error(error_msg)
            repository.update_order_status(db_order.id, "rejected")
            repository.log_event("ERROR", "order_manager", error_msg)
            return None, error_msg

    def get_order_status(self, broker_order_id: str) -> Optional[str]:
        """
        Запросить актуальный статус ордера у брокера.
        Используется для проверки исполнения отложенных ордеров.
        """
        try:
            if settings.USE_SANDBOX:
                with SandboxClient(settings.TINKOFF_TOKEN) as client:
                    response = client.sandbox.get_sandbox_order_state(
                        account_id=self.account_id,
                        order_id=broker_order_id,
                    )
            else:
                with Client(settings.TINKOFF_TOKEN) as client:
                    response = client.orders.get_order_state(
                        account_id=self.account_id,
                        order_id=broker_order_id,
                    )
            status_map = {1: "new", 2: "pending", 3: "cancelled", 4: "filled", 5: "rejected"}
            return status_map.get(response.execution_report_status, "unknown")
        except Exception as e:
            logger.error(f"Ошибка получения статуса ордера {broker_order_id}: {e}")
            return None

    def cancel_order(self, broker_order_id: str) -> bool:
        """Отменить активный ордер. Возвращает True при успехе."""
        try:
            if settings.USE_SANDBOX:
                with SandboxClient(settings.TINKOFF_TOKEN) as client:
                    client.sandbox.cancel_sandbox_order(
                        account_id=self.account_id,
                        order_id=broker_order_id,
                    )
            else:
                with Client(settings.TINKOFF_TOKEN) as client:
                    client.orders.cancel_order(
                        account_id=self.account_id,
                        order_id=broker_order_id,
                    )
            logger.info(f"Ордер {broker_order_id} отменён")
            db_order = repository.get_order_by_broker_id(broker_order_id)
            if db_order:
                repository.update_order_status(db_order.id, "cancelled")
            return True
        except Exception as e:
            logger.error(f"Ошибка отмены ордера {broker_order_id}: {e}")
            return False
