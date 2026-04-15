"""
Стратегия на основе Augmented RSI (Ultimate RSI, LuxAlgo) на 5-минутных свечах.

Алгоритм (Pine Script → Python):
  upper = highest(close, length)
  lower = lowest(close, length)
  r = upper - lower
  d = close - close[-1]
  diff = r  если upper > upper[-1]
       = -r если lower < lower[-1]
       = d  иначе
  num    = ma(diff, length)         # по умолчанию RMA
  den    = ma(abs(diff), length)
  arsi   = num / den * 50 + 50      # диапазон [0, 100]
  signal = ma(arsi, smooth)         # по умолчанию EMA

Входы:
  LONG  — arsi пересекает os_value снизу вверх (выход из перепроданности)
  SHORT — arsi пересекает ob_value сверху вниз (выход из перекупленности)

Выходы по сигналу стратегии (дополнительно к стоп/тейк от PositionManager):
  LONG  → EXIT когда arsi пересекает signal line сверху вниз
  SHORT → EXIT когда arsi пересекает signal line снизу вверх
"""
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Optional

from trading_bot.core.strategy.base_strategy import BaseStrategy, Signal, SignalReason, SignalType
from trading_bot.core.strategy.candle_aggregator import Candle, CandleAggregator

logger = logging.getLogger(__name__)

MSK_OFFSET_HOURS = 3


class _MAState:
    """Инкрементальный вычислитель скользящей средней (EMA / RMA / SMA / TMA)."""

    def __init__(self, length: int, ma_type: str) -> None:
        self.length = length
        self.ma_type = ma_type.upper()
        self._buf: Deque[float] = deque(maxlen=length)
        self._val: Optional[float] = None
        if self.ma_type == "RMA":
            self._alpha = 1.0 / length
        elif self.ma_type == "EMA":
            self._alpha = 2.0 / (length + 1)
        else:
            self._alpha = 0.0
        if self.ma_type == "TMA":
            self._buf2: Deque[float] = deque(maxlen=length)

    def update(self, x: float) -> float:
        if self.ma_type in ("RMA", "EMA"):
            if self._val is None:
                self._val = x
            else:
                self._val = self._alpha * x + (1.0 - self._alpha) * self._val
        elif self.ma_type == "SMA":
            self._buf.append(x)
            self._val = sum(self._buf) / len(self._buf)
        else:  # TMA: sma(sma(x, len), len)
            self._buf.append(x)
            sma1 = sum(self._buf) / len(self._buf)
            self._buf2.append(sma1)
            self._val = sum(self._buf2) / len(self._buf2)
        return self._val  # type: ignore[return-value]

    @property
    def value(self) -> Optional[float]:
        return self._val

    def reset(self) -> None:
        self._buf.clear()
        self._val = None
        if self.ma_type == "TMA" and hasattr(self, "_buf2"):
            self._buf2.clear()


class AugmentedRSI:
    """
    Вычисляет Augmented RSI (LuxAlgo) инкрементально по ценам закрытия свечей.
    """

    def __init__(
        self,
        length: int = 14,
        smooth: int = 14,
        smo_type_rsi: str = "RMA",
        smo_type_signal: str = "EMA",
    ) -> None:
        self.length = length
        self._closes: Deque[float] = deque(maxlen=length)
        self._ma_num = _MAState(length, smo_type_rsi)
        self._ma_den = _MAState(length, smo_type_rsi)
        self._ma_signal = _MAState(smooth, smo_type_signal)

        self._prev_upper: Optional[float] = None
        self._prev_lower: Optional[float] = None
        self._prev_close: Optional[float] = None

        self.last_arsi: Optional[float] = None
        self.last_signal: Optional[float] = None

    def update(self, close: float) -> Optional[tuple]:
        """
        Обновить с новой ценой закрытия свечи.
        Возвращает (arsi, signal_line) или None если данных ещё недостаточно.
        """
        self._closes.append(close)

        if len(self._closes) < 2:
            self._prev_close = close
            return None

        upper = max(self._closes)
        lower = min(self._closes)
        r = upper - lower
        d = close - self._prev_close  # type: ignore[operator]

        if self._prev_upper is None:
            diff = d
        elif upper > self._prev_upper:
            diff = r
        elif lower < self._prev_lower:  # type: ignore[operator]
            diff = -r
        else:
            diff = d

        self._prev_upper = upper
        self._prev_lower = lower
        self._prev_close = close

        num = self._ma_num.update(diff)
        den = self._ma_den.update(abs(diff))

        if den == 0:
            return None

        arsi = (num / den) * 50.0 + 50.0
        arsi = max(0.0, min(100.0, arsi))

        signal = self._ma_signal.update(arsi)

        self.last_arsi = arsi
        self.last_signal = signal
        return arsi, signal

    @property
    def is_ready(self) -> bool:
        return self.last_arsi is not None

    def reset(self) -> None:
        self._closes.clear()
        self._ma_num.reset()
        self._ma_den.reset()
        self._ma_signal.reset()
        self._prev_upper = None
        self._prev_lower = None
        self._prev_close = None
        self.last_arsi = None
        self.last_signal = None


class RSIStrategy(BaseStrategy):
    """
    Стратегия на Augmented RSI (LuxAlgo) на 5-минутных свечах.

    Совместима с BaseStrategy контрактом:
      - on_trade()     → кормит CandleAggregator
      - on_orderbook() → no-op (стакан для этой стратегии не нужен)
      - get_signal()   → возвращает накопленный сигнал и сбрасывает его
    """

    def __init__(self, instrument_config: Dict[str, Any]) -> None:
        # Инициализируем RSI до вызова super().__init__,
        # так как load_params обращается к self._rsi.
        self._rsi: AugmentedRSI = AugmentedRSI()
        self._aggregator: CandleAggregator = CandleAggregator(
            interval_minutes=5,
            on_candle_closed=self._on_candle_closed,
        )
        self._signal: Optional[Signal] = None
        self._position_direction: Optional[str] = None
        self._last_entry_time: Optional[datetime] = None
        self._last_close_time: Optional[datetime] = None
        self._prev_arsi: Optional[float] = None
        self._prev_signal_line: Optional[float] = None

        # ATR-фильтр активности: обновляется извне каждые 5 минут через update_atr()
        self._atr_short: Optional[float] = None
        self._atr_long: Optional[float] = None

        super().__init__(instrument_config)

    def load_params(self, instrument_config: Dict[str, Any]) -> None:
        super().load_params(instrument_config)
        if hasattr(self, "_rsi"):
            self._rsi = AugmentedRSI(
                length=self.params.get("length", 14),
                smooth=self.params.get("smooth", 14),
                smo_type_rsi=self.params.get("smo_type_rsi", "RMA"),
                smo_type_signal=self.params.get("smo_type_signal", "EMA"),
            )

    # ── BaseStrategy interface ────────────────────────────────────────────────

    def on_orderbook(self, orderbook_data: Dict[str, Any]) -> None:
        pass  # RSI стратегия не использует данные стакана

    def on_trade(self, trade_data: Dict[str, Any]) -> None:
        price: float = trade_data.get("price", 0.0)
        quantity: int = trade_data.get("quantity", 0)
        ts: datetime = trade_data.get("time", datetime.utcnow())
        if price > 0 and quantity > 0:
            self._aggregator.on_trade(price=price, volume=float(quantity), timestamp=ts)

    def get_signal(self) -> Optional[Signal]:
        sig = self._signal
        self._signal = None
        return sig

    # ── Position state ────────────────────────────────────────────────────────

    def warmup(self, closes: list) -> None:
        """
        Прогреть RSI на исторических свечах перед началом торговли.
        Прогоняет все closes через AugmentedRSI чтобы RMA сошлась
        и значения совпали с TradingView.
        После прогрева _prev_arsi содержит arsi последней исторической свечи.
        """
        for close in closes:
            result = self._rsi.update(close)
            if result is not None:
                self._prev_arsi, self._prev_signal_line = result
        if self._rsi.last_arsi is not None:
            logger.info(
                f"RSI прогрет на {len(closes)} свечах: "
                f"arsi={self._rsi.last_arsi:.1f}, signal={self._rsi.last_signal:.1f}"
            )
        else:
            logger.warning(f"RSI прогрев: недостаточно данных ({len(closes)} свечей)")

    def update_atr(self, short_atr: float, long_atr: float) -> None:
        """Обновить значения ATR (вызывается из планировщика каждые 5 минут)."""
        self._atr_short = short_atr
        self._atr_long = long_atr
        logger.debug(f"ATR обновлён: short={short_atr:.4f}, long={long_atr:.4f}, ratio={short_atr/long_atr:.2f}" if long_atr > 0 else f"ATR обновлён: short={short_atr:.4f}, long={long_atr:.4f}")

    def set_position(
        self,
        direction: Optional[str],
        close_time: Optional[datetime] = None,
    ) -> None:
        """Уведомить стратегию об изменении позиции (вызывается из PositionManager)."""
        if direction is None and self._position_direction is not None:
            self._last_close_time = close_time or datetime.utcnow()
        self._position_direction = direction

    # ── Internal candle handler ───────────────────────────────────────────────

    def _on_candle_closed(self, candle: Candle) -> None:
        """Вызывается CandleAggregator когда 5-минутная свеча закрылась."""
        result = self._rsi.update(candle.close)
        if result is None:
            return

        arsi, signal_line = result

        if self._prev_arsi is None:
            # Первое реальное значение — только сохраняем состояние
            self._prev_arsi = arsi
            self._prev_signal_line = signal_line
            return

        now = candle.open_time
        ob = self.params.get("ob_value", 80.0)
        os_ = self.params.get("os_value", 20.0)

        if not self._is_trading_hours(now):
            self._prev_arsi = arsi
            self._prev_signal_line = signal_line
            return

        if self._position_direction is None:
            self._try_entry(arsi, signal_line, now, ob, os_)
        else:
            self._try_exit(arsi, signal_line, now)

        self._prev_arsi = arsi
        self._prev_signal_line = signal_line

    def _try_entry(
        self,
        arsi: float,
        signal_line: float,
        now: datetime,
        ob: float,
        os_: float,
    ) -> None:
        """Проверить условие входа."""
        cooldown = self.params.get("cooldown_seconds", 300)
        post_close_cd = self.params.get("post_close_cooldown_seconds", 600)

        if self._last_entry_time is not None:
            if (now - self._last_entry_time).total_seconds() < cooldown:
                return

        if self._last_close_time is not None:
            if (now - self._last_close_time).total_seconds() < post_close_cd:
                return

        # ATR-фильтр: блокируем вход если рынок неактивен
        atr_ratio_min = self.params.get("atr_ratio_min", 0.0)
        if atr_ratio_min > 0:
            if self._atr_short is None or self._atr_long is None:
                logger.debug("ATR ещё не загружен, вход заблокирован")
                return
            if self._atr_long <= 0:
                logger.debug("ATR long = 0, вход заблокирован")
                return
            ratio = self._atr_short / self._atr_long
            if ratio < atr_ratio_min:
                logger.debug(
                    f"ATR-фильтр: рынок неактивен (short={self._atr_short:.4f}, "
                    f"long={self._atr_long:.4f}, ratio={ratio:.2f} < {atr_ratio_min})"
                )
                return

        prev = self._prev_arsi
        assert prev is not None

        # LONG: arsi пересекает os_ снизу вверх
        if prev < os_ and arsi >= os_:
            self._signal = Signal(
                signal_type=SignalType.LONG,
                reason=SignalReason.COMBO_TRIGGERED,
                ofi_value=arsi,
                print_volume=signal_line,
                timestamp=now,
            )
            self._last_entry_time = now
            logger.info(
                f"RSI LONG: arsi={arsi:.1f} пересёк OS {os_:.0f} снизу вверх"
            )

        # SHORT: arsi пересекает ob_ сверху вниз
        elif prev > ob and arsi <= ob:
            self._signal = Signal(
                signal_type=SignalType.SHORT,
                reason=SignalReason.COMBO_TRIGGERED,
                ofi_value=arsi,
                print_volume=signal_line,
                timestamp=now,
            )
            self._last_entry_time = now
            logger.info(
                f"RSI SHORT: arsi={arsi:.1f} пересёк OB {ob:.0f} сверху вниз"
            )

    def _try_exit(
        self,
        arsi: float,
        signal_line: float,
        now: datetime,
    ) -> None:
        """
        Проверить условие выхода из позиции по пересечению ARSI и signal line.

        LONG  → EXIT когда arsi пересекает signal line сверху вниз
                (prev_arsi >= prev_signal_line AND arsi < signal_line)
        SHORT → EXIT когда arsi пересекает signal line снизу вверх
                (prev_arsi <= prev_signal_line AND arsi > signal_line)
        """
        prev_arsi = self._prev_arsi
        prev_sig = self._prev_signal_line
        if prev_arsi is None or prev_sig is None:
            return

        crossed = False
        if self._position_direction == "long":
            crossed = prev_arsi >= prev_sig and arsi < signal_line
        elif self._position_direction == "short":
            crossed = prev_arsi <= prev_sig and arsi > signal_line

        if crossed:
            self._signal = Signal(
                signal_type=SignalType.EXIT,
                reason=SignalReason.OFI_REVERSED,
                ofi_value=arsi,
                print_volume=signal_line,
                timestamp=now,
            )
            logger.info(
                f"RSI EXIT ({self._position_direction}): arsi={arsi:.1f} пересёк "
                f"signal={signal_line:.1f} ({'↓' if self._position_direction == 'long' else '↑'})"
            )

    def _is_trading_hours(self, ts: datetime) -> bool:
        """Проверить что время (UTC datetime) попадает в торговые часы (MSK)."""
        trading_hours = self.params.get("trading_hours", {})
        start_str = trading_hours.get("start", "10:05")
        end_str = trading_hours.get("end", "18:30")
        skip_first = self.params.get("skip_first_minutes", 5)

        msk = ts + timedelta(hours=MSK_OFFSET_HOURS)
        current_min = msk.hour * 60 + msk.minute

        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start_min = sh * 60 + sm + skip_first
        end_min = eh * 60 + em

        return start_min <= current_min < end_min

    @property
    def current_ofi(self) -> Optional[float]:
        """Последнее значение ARSI (аналог OFI для совместимости с PositionManager)."""
        return self._rsi.last_arsi

    def reset(self) -> None:
        self._rsi.reset()
        self._aggregator.reset()
        self._signal = None
        self._position_direction = None
        self._last_entry_time = None
        self._last_close_time = None
        self._prev_arsi = None
        self._prev_signal_line = None
