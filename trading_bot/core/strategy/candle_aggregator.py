"""
Агрегатор свечей — собирает сделки в OHLCV-свечи фиксированного периода.
"""
import logging
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class Candle:
    """Одна OHLCV-свеча."""
    __slots__ = ("open_time", "open", "high", "low", "close", "volume", "trade_count")

    def __init__(self, open_time: datetime, price: float, volume: float) -> None:
        self.open_time = open_time
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.volume = volume
        self.trade_count = 1

    def update(self, price: float, volume: float) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close = price
        self.volume += volume
        self.trade_count += 1


class CandleAggregator:
    """
    Агрегирует тиковые сделки в свечи фиксированного периода (в минутах).

    Когда текущий период закрывается (приходит тик в новом периоде),
    вызывает on_candle_closed(candle) с завершённой свечой.
    """

    def __init__(
        self,
        interval_minutes: int,
        on_candle_closed: Callable[["Candle"], None],
    ) -> None:
        self.interval_minutes = interval_minutes
        self.on_candle_closed = on_candle_closed
        self._current_candle: Optional[Candle] = None
        self._current_key: Optional[int] = None  # минут с начала суток, округлённых до интервала

    def _period_key(self, ts: datetime) -> int:
        """Ключ периода: минуты с полуночи UTC, округлённые до интервала."""
        minutes = ts.hour * 60 + ts.minute
        return (minutes // self.interval_minutes) * self.interval_minutes

    def _candle_open_time(self, ts: datetime, key: int) -> datetime:
        """Время открытия свечи для заданного ключа периода."""
        return ts.replace(hour=key // 60, minute=key % 60, second=0, microsecond=0)

    def on_trade(self, price: float, volume: float, timestamp: datetime) -> None:
        """Обработать сделку. При закрытии периода вызывает on_candle_closed."""
        key = self._period_key(timestamp)

        if self._current_key is None:
            # Первый тик — открываем свечу
            self._current_key = key
            self._current_candle = Candle(
                open_time=self._candle_open_time(timestamp, key),
                price=price,
                volume=volume,
            )
        elif key != self._current_key:
            # Новый период — закрываем предыдущую свечу и открываем новую
            if self._current_candle is not None:
                try:
                    self.on_candle_closed(self._current_candle)
                except Exception:
                    logger.exception("Ошибка в обработчике закрытия свечи")
            self._current_key = key
            self._current_candle = Candle(
                open_time=self._candle_open_time(timestamp, key),
                price=price,
                volume=volume,
            )
        else:
            # Тот же период — обновляем текущую свечу
            if self._current_candle is not None:
                self._current_candle.update(price, volume)

    def reset(self) -> None:
        self._current_candle = None
        self._current_key = None
