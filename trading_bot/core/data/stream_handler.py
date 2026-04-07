"""
Обработчик стриминговых данных от T-Invest API.

Подписывается на стрим стакана и сделок для заданного инструмента,
нормализует данные и передаёт их в стратегию и менеджер позиций.

При разрыве соединения автоматически переподключается с экспоненциальной
задержкой (backoff) — устойчивость к временным сетевым проблемам.
"""
import logging
import time
from typing import Any, Callable, Dict, Optional

from tinkoff.invest import (
    Client,
    MarketDataRequest,
    OrderBookInstrument,
    SubscribeOrderBookRequest,
    SubscribeTradesRequest,
    SubscriptionAction,
    TradeInstrument,
)
from tinkoff.invest.market_data_stream.market_data_stream_service import MarketDataStreamService

from trading_bot.config import settings
from trading_bot.core.data.market_data import normalize_orderbook, normalize_trade

logger = logging.getLogger(__name__)


class StreamHandler:
    """
    Управляет стримом рыночных данных для одного инструмента.

    Callbacks:
      on_orderbook(data) — вызывается при каждом обновлении стакана
      on_trade(data)     — вызывается при каждой сделке
    """

    def __init__(
        self,
        figi: str,
        on_orderbook: Callable[[Dict[str, Any]], None],
        on_trade: Callable[[Dict[str, Any]], None],
        orderbook_depth: int = 10,
    ) -> None:
        self.figi = figi
        self._on_orderbook = on_orderbook
        self._on_trade = on_trade
        self.orderbook_depth = orderbook_depth
        self._running = False

    def start(self) -> None:
        """
        Запустить стрим с автоматическим переподключением.

        Блокирующий вызов — должен запускаться в отдельном потоке.
        """
        self._running = True
        backoff = 1  # начальная задержка переподключения в секундах

        while self._running:
            try:
                logger.info(f"Подключение к стриму: figi={self.figi}")
                self._run_stream()
                backoff = 1  # успешный запуск сбрасывает backoff
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"Ошибка стрима: {e}. Переподключение через {backoff}с...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)  # максимум 60 секунд

    def stop(self) -> None:
        """Остановить стрим."""
        self._running = False
        logger.info(f"Стрим остановлен: figi={self.figi}")

    def _run_stream(self) -> None:
        """Внутренний цикл стрима — читает события и диспатчит их."""
        with Client(settings.TINKOFF_TOKEN) as client:
            stream: MarketDataStreamService = client.create_market_data_stream()

            # Подписываемся на стакан
            stream.subscribe_order_book(
                instruments=[
                    OrderBookInstrument(figi=self.figi, depth=self.orderbook_depth)
                ],
                subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            )

            # Подписываемся на поток сделок
            stream.subscribe_trades(
                instruments=[TradeInstrument(figi=self.figi)],
                subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            )

            logger.info(f"Стрим активен: figi={self.figi}")

            for market_data in stream:
                if not self._running:
                    break

                if market_data.orderbook:
                    try:
                        normalized = normalize_orderbook(market_data.orderbook)
                        self._on_orderbook(normalized)
                    except Exception as e:
                        logger.error(f"Ошибка обработки стакана: {e}")

                elif market_data.trade:
                    try:
                        normalized = normalize_trade(market_data.trade)
                        self._on_trade(normalized)
                    except Exception as e:
                        logger.error(f"Ошибка обработки сделки: {e}")
