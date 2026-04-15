"""
Менеджер портфеля.

Отслеживает стоимость счёта и глобальный лимит открытых позиций.
Один экземпляр на весь бот — разделяется между всеми PositionManager.
"""
import logging
import threading
from typing import Dict, List, Optional

from tinkoff.invest import Client
from tinkoff.invest.sandbox.client import SandboxClient
from tinkoff.invest.utils import quotation_to_decimal

from trading_bot.config import settings

logger = logging.getLogger(__name__)


class PortfolioManager:
    """
    Общий менеджер портфеля для всех инструментов.

    Функции:
    - Получает текущую стоимость счёта из T-Invest API (обновляется по расписанию)
    - Запрашивает последние цены всех инструментов из API (актуальнее чем хардкод)
    - Отслеживает кол-во открытых позиций по всем тикерам
    - Рассчитывает доступный размер лота (N% от портфеля)
    - Блокирует открытие если достигнут лимит одновременных позиций
    """

    def __init__(
        self,
        account_id: str,
        max_positions: int,
        max_position_pct: float,
        figis: Optional[List[str]] = None,
    ) -> None:
        self._account_id = account_id
        self._max_positions = max_positions
        self._max_position_pct = max_position_pct
        self._portfolio_value: float = 0.0
        # figi → последняя известная цена из API
        self._last_prices: Dict[str, float] = {}
        self._figis: List[str] = figis or []
        # Множество instrument_id с открытыми позициями
        self._open_ids: set = set()
        self._lock = threading.Lock()

    # ── Portfolio value ─────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Обновить стоимость портфеля и последние цены из T-Invest API."""
        self._refresh_portfolio()
        if self._figis:
            self._refresh_prices()

    def _refresh_portfolio(self) -> None:
        try:
            if settings.USE_SANDBOX:
                with SandboxClient(settings.TINKOFF_TOKEN) as client:
                    resp = client.sandbox.get_sandbox_portfolio(account_id=self._account_id)
            else:
                with Client(settings.TINKOFF_TOKEN) as client:
                    resp = client.operations.get_portfolio(account_id=self._account_id)

            value = float(quotation_to_decimal(resp.total_amount_portfolio))
            with self._lock:
                self._portfolio_value = value
            logger.debug(f"Портфель обновлён: {value:.2f} руб.")
        except Exception as e:
            logger.error(f"Ошибка обновления стоимости портфеля: {e}")

    def _refresh_prices(self) -> None:
        """Запросить последние цены инструментов из market_data API."""
        try:
            # get_last_prices работает одинаково в sandbox и prod
            if settings.USE_SANDBOX:
                with SandboxClient(settings.TINKOFF_TOKEN) as client:
                    resp = client.market_data.get_last_prices(figi=self._figis)
            else:
                with Client(settings.TINKOFF_TOKEN) as client:
                    resp = client.market_data.get_last_prices(figi=self._figis)

            prices = {}
            for lp in resp.last_prices:
                price = float(quotation_to_decimal(lp.price))
                if price > 0:
                    prices[lp.figi] = price

            with self._lock:
                self._last_prices.update(prices)

            price_log = ", ".join(f"{figi[-4:]}={p:.2f}" for figi, p in prices.items())
            logger.debug(f"Цены обновлены: {price_log}")
        except Exception as e:
            logger.error(f"Ошибка обновления цен инструментов: {e}")

    def get_price(self, figi: str) -> Optional[float]:
        """Последняя известная цена инструмента из API (None если не загружена)."""
        with self._lock:
            return self._last_prices.get(figi)

    @property
    def portfolio_value(self) -> float:
        with self._lock:
            return self._portfolio_value

    # ── Position tracking ───────────────────────────────────────────────────────

    def try_register_opened(self, instrument_id: int, strategy_name: str) -> bool:
        """
        Атомарно проверить лимит и зарегистрировать позицию.

        Возвращает True если позиция зарегистрирована, False если лимит достигнут.
        Проверка и регистрация выполняются под одним локом — нет race condition
        между параллельными потоками разных тикеров.
        """
        key = (instrument_id, strategy_name)
        with self._lock:
            if len(self._open_ids) >= self._max_positions:
                return False
            self._open_ids.add(key)
        logger.debug(
            f"Позиция зарегистрирована ({strategy_name}/{instrument_id}), "
            f"всего открыто: {len(self._open_ids)}"
        )
        return True

    def register_closed(self, instrument_id: int, strategy_name: str = "") -> None:
        """Снять регистрацию закрытой позиции."""
        key = (instrument_id, strategy_name)
        with self._lock:
            self._open_ids.discard(key)
            # Обратная совместимость: убираем и старый формат int если вдруг остался
            self._open_ids.discard(instrument_id)
        logger.debug(
            f"Позиция снята ({strategy_name}/{instrument_id}), "
            f"всего открыто: {len(self._open_ids)}"
        )

    @property
    def open_positions_count(self) -> int:
        with self._lock:
            return len(self._open_ids)

    def can_open(self) -> bool:
        """Можно ли открыть ещё одну позицию (глобальный лимит). Только для чтения."""
        return self.open_positions_count < self._max_positions

    # ── Lot size calculation ────────────────────────────────────────────────────

    def compute_lots(
        self,
        figi: str,
        lot_size: int,
        stream_price: float = 0.0,
        max_lots_cap: Optional[int] = None,
    ) -> int:
        """
        Рассчитать кол-во лотов для новой позиции.

        Приоритет цены:
          1. stream_price — свежая цена из торгового стрима (если > 0)
          2. _last_prices[figi] — цена из последнего API refresh
          3. fallback: max_lots_cap или 1 (логирует WARNING)

        Логика: берём max_position_pct от портфеля и делим на стоимость одного лота.
        max_lots_cap — жёсткий потолок из конфига инструмента (опционально).
        """
        with self._lock:
            portfolio = self._portfolio_value
            api_price = self._last_prices.get(figi, 0.0)

        price = stream_price if stream_price > 0 else api_price

        if portfolio <= 0 or price <= 0 or lot_size <= 0:
            fallback = max_lots_cap if (max_lots_cap and max_lots_cap > 0) else 1
            logger.warning(
                f"Портфель не загружен или цена неизвестна (figi={figi}) — "
                f"используем {fallback} лот(ов)"
            )
            return fallback

        max_value = portfolio * self._max_position_pct
        lots = int(max_value / (price * lot_size))
        lots = max(1, lots)

        if max_lots_cap and max_lots_cap > 0:
            lots = min(lots, max_lots_cap)

        source = "стрим" if stream_price > 0 else "API"
        logger.debug(
            f"Размер позиции [{figi[-4:]}]: портфель={portfolio:.0f}₽, "
            f"лимит={max_value:.0f}₽ ({self._max_position_pct*100:.0f}%), "
            f"цена={price:.2f}×{lot_size}лот [{source}] → {lots} лот(ов)"
        )
        return lots

    def get_summary(self) -> dict:
        """Сводка для дашборда."""
        with self._lock:
            return {
                "portfolio_value": round(self._portfolio_value, 2),
                "open_positions": len(self._open_ids),
                "max_positions": self._max_positions,
                "max_position_pct": self._max_position_pct,
            }
