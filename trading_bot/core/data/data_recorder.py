"""
Запись рыночных данных в БД для последующего бэктеста.

Включается через RECORD_MARKET_DATA=true в .env.
Подключается к тем же колбэкам что и стратегия — параллельно, без вмешательства.
Запись ведётся только в рабочее время инструмента (trading_hours из конфига).
"""
import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional

from trading_bot.config import settings
from trading_bot.db import repository

logger = logging.getLogger(__name__)


class DataRecorder:
    """
    Записывает снапшоты стакана и тиковые сделки в БД.

    Используется для накопления исторических данных для бэктеста.
    При RECORD_MARKET_DATA=false методы работают как no-op.
    Запись ведётся только в пределах trading_hours (MSK) из instrument_config.
    """

    def __init__(self, figi: str, instrument_config: Optional[Dict[str, Any]] = None) -> None:
        self.figi = figi
        self._ob_counter = 0
        self._enabled = settings.RECORD_MARKET_DATA
        self._interval = max(1, settings.RECORD_ORDERBOOK_INTERVAL)

        hours = (instrument_config or {}).get("trading_hours", {})
        self._trading_start: Optional[time] = self._parse_time(hours.get("start"))
        self._trading_end: Optional[time] = self._parse_time(hours.get("end"))

        if self._enabled:
            logger.info(
                f"DataRecorder активен: figi={figi}, "
                f"интервал стакана={self._interval}, "
                f"рабочие часы (MSK): {self._trading_start}–{self._trading_end}"
            )
        else:
            logger.info("DataRecorder отключён (RECORD_MARKET_DATA=false)")

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[time]:
        if not value:
            return None
        try:
            h, m = value.split(":")
            return time(int(h), int(m))
        except (ValueError, AttributeError):
            return None

    def _is_trading_hours(self, timestamp: datetime) -> bool:
        """Вернуть True если timestamp (UTC) попадает в рабочие часы (MSK)."""
        if self._trading_start is None or self._trading_end is None:
            return True  # конфиг не задан — пишем всегда
        moscow_time = (timestamp + timedelta(hours=3)).time()
        return self._trading_start <= moscow_time <= self._trading_end

    def on_orderbook(self, data: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        if not self._is_trading_hours(data["time"]):
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
        if not self._is_trading_hours(data["time"]):
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
