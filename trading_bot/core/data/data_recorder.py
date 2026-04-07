"""
Запись рыночных данных в БД для последующего бэктеста.

Включается через RECORD_MARKET_DATA=true в .env.
Подключается к тем же колбэкам что и стратегия — параллельно, без вмешательства.
"""
import logging
from typing import Any, Dict

from trading_bot.config import settings
from trading_bot.db import repository

logger = logging.getLogger(__name__)


class DataRecorder:
    """
    Записывает снапшоты стакана и тиковые сделки в БД.

    Используется для накопления исторических данных для бэктеста.
    При RECORD_MARKET_DATA=false методы работают как no-op.
    """

    def __init__(self, figi: str) -> None:
        self.figi = figi
        self._ob_counter = 0
        self._enabled = settings.RECORD_MARKET_DATA
        self._interval = max(1, settings.RECORD_ORDERBOOK_INTERVAL)

        if self._enabled:
            logger.info(
                f"DataRecorder активен: figi={figi}, "
                f"интервал стакана={self._interval}"
            )
        else:
            logger.info("DataRecorder отключён (RECORD_MARKET_DATA=false)")

    def on_orderbook(self, data: Dict[str, Any]) -> None:
        if not self._enabled:
            return

        self._ob_counter += 1
        if self._ob_counter % self._interval != 0:
            return

        try:
            repository.save_orderbook_snapshot(
                figi=data["figi"],
                bids=data["bids"],
                asks=data["asks"],
                timestamp=data["time"],
            )
        except Exception as e:
            logger.error(f"DataRecorder: ошибка записи стакана: {e}")

    def on_trade(self, data: Dict[str, Any]) -> None:
        if not self._enabled:
            return

        try:
            repository.save_trade_tick(
                figi=data["figi"],
                price=data["price"],
                quantity=data["quantity"],
                direction=data["direction"],
                timestamp=data["time"],
            )
        except Exception as e:
            logger.error(f"DataRecorder: ошибка записи тика: {e}")
