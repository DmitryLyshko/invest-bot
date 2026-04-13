"""
Менеджер портфеля.

Отслеживает стоимость счёта и глобальный лимит открытых позиций.
Один экземпляр на весь бот — разделяется между всеми PositionManager.
"""
import logging
import threading
from typing import Optional

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
    - Отслеживает кол-во открытых позиций по всем тикерам
    - Рассчитывает доступный размер лота (N% от портфеля)
    - Блокирует открытие если достигнут лимит одновременных позиций
    """

    def __init__(
        self,
        account_id: str,
        max_positions: int,
        max_position_pct: float,
    ) -> None:
        self._account_id = account_id
        self._max_positions = max_positions
        self._max_position_pct = max_position_pct
        self._portfolio_value: float = 0.0
        # Множество instrument_id с открытыми позициями
        self._open_ids: set = set()
        self._lock = threading.Lock()

    # ── Portfolio value ─────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Обновить стоимость портфеля из T-Invest API."""
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

    @property
    def portfolio_value(self) -> float:
        with self._lock:
            return self._portfolio_value

    # ── Position tracking ───────────────────────────────────────────────────────

    def register_opened(self, instrument_id: int) -> None:
        """Зарегистрировать открытую позицию."""
        with self._lock:
            self._open_ids.add(instrument_id)
        logger.debug(
            f"Позиция зарегистрирована (instrument_id={instrument_id}), "
            f"всего открыто: {len(self._open_ids)}"
        )

    def register_closed(self, instrument_id: int) -> None:
        """Снять регистрацию закрытой позиции."""
        with self._lock:
            self._open_ids.discard(instrument_id)
        logger.debug(
            f"Позиция снята (instrument_id={instrument_id}), "
            f"всего открыто: {len(self._open_ids)}"
        )

    @property
    def open_positions_count(self) -> int:
        with self._lock:
            return len(self._open_ids)

    def can_open(self) -> bool:
        """Можно ли открыть ещё одну позицию (глобальный лимит)."""
        return self.open_positions_count < self._max_positions

    # ── Lot size calculation ────────────────────────────────────────────────────

    def compute_lots(
        self,
        current_price: float,
        lot_size: int,
        max_lots_cap: Optional[int] = None,
    ) -> int:
        """
        Рассчитать кол-во лотов для новой позиции.

        Логика: берём max_position_pct от портфеля и делим на стоимость одного лота.
        Если стоимость портфеля ещё не загружена — возвращаем 1 (безопасный минимум).

        max_lots_cap — жёсткий потолок из конфига инструмента (опционально).
        """
        with self._lock:
            portfolio = self._portfolio_value

        if portfolio <= 0 or current_price <= 0 or lot_size <= 0:
            fallback = max_lots_cap if (max_lots_cap and max_lots_cap > 0) else 1
            logger.warning(
                f"Портфель не загружен или нулевая цена — используем {fallback} лот(ов)"
            )
            return fallback

        max_value = portfolio * self._max_position_pct
        lots = int(max_value / (current_price * lot_size))
        lots = max(1, lots)

        if max_lots_cap and max_lots_cap > 0:
            lots = min(lots, max_lots_cap)

        logger.debug(
            f"Размер позиции: портфель={portfolio:.0f}₽, "
            f"лимит={max_value:.0f}₽ ({self._max_position_pct*100:.0f}%), "
            f"цена={current_price:.2f}×{lot_size}лот → {lots} лот(ов)"
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
