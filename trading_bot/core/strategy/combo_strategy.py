"""
Комбинированная стратегия OFI + Крупный Принт.

Идея стратегии:
  Крупный принт сам по себе может быть ложным сигналом (информационный шум,
  хеджирование, разбивка крупного ордера). Но если крупный принт на покупку
  сопровождается сильным положительным OFI — это синхронное давление покупателей
  по двум независимым метрикам. Вероятность краткосрочного движения вверх резко
  возрастает.

Логика входа:
  LONG:  OFI >= +threshold И последний принт был на покупку
  SHORT: OFI <= -threshold И последний принт был на продажу

Логика выхода:
  Выход только через stop-loss / take-profit / trailing-stop / timeout (PositionManager).
  OFI-разворот как причина выхода — отключён.

Фильтр активности рынка:
  Если диапазон mid-цены за последние activity_window снапшотов стакана
  меньше min_activity_range_ticks * tick_size — рынок "стоит", входы блокируются.
  Параметры: activity_window (default 0 = выкл), min_activity_range_ticks (default 0).
"""
import logging
from collections import deque
from datetime import datetime, time, timedelta
from typing import Any, Deque, Dict, Optional

from trading_bot.core.strategy.base_strategy import (
    BaseStrategy, Signal, SignalReason, SignalType,
)
from trading_bot.core.strategy.ofi_calculator import OFICalculator
from trading_bot.core.strategy.print_detector import PrintDetector, PrintEvent

logger = logging.getLogger(__name__)


class ComboStrategy(BaseStrategy):
    """
    Стратегия, генерирующая сигналы при совпадении OFI и крупного принта.
    """

    def __init__(self, instrument_config: Dict[str, Any]) -> None:
        super().__init__(instrument_config)

        # Создаём компоненты стратегии с параметрами из конфига.
        # smooth_window — окно сглаживания OFI; если не задан в конфиге, используем 1
        # (отключено — поведение как раньше, для обратной совместимости).
        self.ofi_calc = OFICalculator(
            ofi_levels=self.params["ofi_levels"],
            smooth_window=self.params.get("ofi_smooth_window", 1),
            ofi_scale=self.params.get("ofi_scale", 1000.0),
            calibrate_window=self.params.get("ofi_auto_calibrate_window", 0),
        )
        self.print_detector = PrintDetector(
            print_window=self.params["print_window"],
            print_multiplier=self.params["print_multiplier"],
        )

        # Накопленный сигнал, который будет прочитан через get_signal()
        self._pending_signal: Optional[Signal] = None

        # Текущее направление открытой позиции: None / "long" / "short"
        # Стратегия должна знать о позиции, чтобы генерировать сигналы выхода
        self._open_position_direction: Optional[str] = None

        # Время последнего сигнала для соблюдения cooldown
        self._last_signal_time: Optional[datetime] = None

        # Время последнего закрытия позиции для post_close_cooldown
        self._last_close_time: Optional[datetime] = None

        # Последнее рассчитанное OFI (для логгирования и отладки)
        self._current_ofi: Optional[float] = None

        # Счётчик и направление подтверждений OFI для ВХОДА.
        # OFI должен держаться выше порога N снимков подряд перед генерацией сигнала.
        # Симметрично логике выхода, защищает от входа на кратковременных всплесках.
        self._ofi_entry_confirmations: int = 0
        self._ofi_entry_direction: Optional[str] = None  # "long" / "short"

        # Фильтр тренда: скользящая средняя mid-цен.
        # trend_ma_window > 0 — включён; 0 — отключён.
        trend_ma_window = self.params.get("trend_ma_window", 0)
        self._trend_ma_window: int = trend_ma_window
        self._mid_history: Optional[Deque[float]] = (
            deque(maxlen=trend_ma_window) if trend_ma_window > 0 else None
        )
        self._current_mid: Optional[float] = None

        # Кэшированные торговые часы (парсятся один раз, а не на каждом тике стакана)
        self._trading_start: Optional[time] = None
        self._trading_end: Optional[time] = None
        self._cache_trading_hours()

    def _cache_trading_hours(self) -> None:
        """Парсим строки торговых часов один раз и сохраняем объекты time."""
        hours = self.params.get("trading_hours", {})
        start_str = hours.get("start", "10:05")
        end_str = hours.get("end", "18:30")
        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))
        self._trading_start = time(start_h, start_m)
        self._trading_end = time(end_h, end_m)

    def load_params(self, instrument_config: Dict[str, Any]) -> None:
        """Перезагрузить параметры и пересоздать компоненты."""
        super().load_params(instrument_config)
        self._cache_trading_hours()
        # Если компоненты уже существуют — обновляем их параметры
        if hasattr(self, "ofi_calc"):
            self.ofi_calc = OFICalculator(
                ofi_levels=self.params["ofi_levels"],
                smooth_window=self.params.get("ofi_smooth_window", 1),
                ofi_scale=self.params.get("ofi_scale", 1000.0),
                calibrate_window=self.params.get("ofi_auto_calibrate_window", 0),
            )
        if hasattr(self, "print_detector"):
            self.print_detector = PrintDetector(
                print_window=self.params["print_window"],
                print_multiplier=self.params["print_multiplier"],
            )
        trend_ma_window = self.params.get("trend_ma_window", 0)
        self._trend_ma_window = trend_ma_window
        self._mid_history = deque(maxlen=trend_ma_window) if trend_ma_window > 0 else None
        self._current_mid = None

    def on_orderbook(self, orderbook_data: Dict[str, Any]) -> None:
        """
        Обработать обновление стакана заявок.

        Шаги:
        1. Обновляем котировки в print_detector (нужны для определения агрессора)
        2. Пересчитываем OFI по новому снапшоту стакана
        3. Если есть открытая позиция — проверяем условие выхода
        4. Если нет позиции — проверяем условие входа (с учётом cooldown)
        """
        bids = orderbook_data.get("bids", [])
        asks = orderbook_data.get("asks", [])
        timestamp: datetime = orderbook_data.get("time", datetime.utcnow())

        # Обновляем котировки для print_detector и фильтра тренда
        if bids and asks:
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            self.print_detector.update_quotes(best_bid, best_ask)
            self._current_mid = (best_bid + best_ask) / 2
            if self._mid_history is not None:
                self._mid_history.append(self._current_mid)
        # Пересчитываем OFI
        ofi = self.ofi_calc.update(bids, asks)
        if ofi is None:
            # Первый снапшот — нет предыдущего состояния для сравнения
            return

        self._current_ofi = ofi
        logger.debug(f"OFI={ofi:.3f}")

        # Проверяем торговые часы
        if not self._is_trading_hours(timestamp):
            return

        # ── Логика входа (только если нет позиции) ────────────────────────
        if self._open_position_direction is None:
            entry_signal = self._check_entry_condition(ofi, timestamp)
            if entry_signal is not None:
                self._pending_signal = entry_signal

    def on_trade(self, trade_data: Dict[str, Any]) -> None:
        """
        Обработать сделку из стрима.
        Передаём данные в print_detector для отслеживания крупных принтов.
        """
        price: float = trade_data["price"]
        volume: float = trade_data["quantity"]
        direction: str = trade_data.get("direction", "unknown")
        timestamp: datetime = trade_data.get("time", datetime.utcnow())

        # Детектор сам решит, является ли это крупным принтом
        self.print_detector.on_trade(price, volume, direction, timestamp)

    def _check_entry_condition(self, ofi: float, timestamp: datetime) -> Optional[Signal]:
        """
        Проверить условие входа в позицию.

        Вход разрешён если:
        1. |OFI| >= threshold (достаточно сильный дисбаланс)
        2. Последний крупный принт совпадает по направлению с OFI
        3. Прошло достаточно времени с последнего сигнала (cooldown)
        4. Детектор принтов накопил достаточно данных
        """
        threshold = self.params["ofi_threshold"]

        # Проверяем cooldown — слишком частые сигналы снижают качество
        if not self._is_cooldown_passed(timestamp):
            return None

        # Принт-детектор должен быть "прогрет"
        if not self.print_detector.window_filled:
            return None

        last_print: Optional[PrintEvent] = self.print_detector.last_print
        if last_print is None:
            # Нет недавнего крупного принта — одного OFI недостаточно
            return None

        # Проверяем свежесть принта: он не должен быть старше print_max_age_seconds.
        # Ликвидные инструменты (SBER) — 15с; менее ликвидные (PLZL, MGNT) — 25-30с.
        print_max_age = self.params.get("print_max_age_seconds", 15)
        print_age = (timestamp - last_print.timestamp).total_seconds()
        if print_age > print_max_age:
            logger.debug(
                f"Принт устарел: возраст {print_age:.1f}с > {print_max_age}с, пропуск"
            )
            return None

        # ── Фильтр тренда ─────────────────────────────────────────────────
        trend_ma: Optional[float] = None
        if self._mid_history is not None and self._current_mid is not None:
            if len(self._mid_history) < self._trend_ma_window:
                return None
            trend_ma = sum(self._mid_history) / len(self._mid_history)

        # ── Подтверждения OFI для входа ───────────────────────────────────
        # OFI должен держаться выше порога min_ofi_entry_confirmations снимков подряд.
        # Защита от входа на кратковременных всплесках стакана.
        min_entry_conf = self.params.get("min_ofi_entry_confirmations", 1)

        if ofi >= threshold:
            ofi_candidate = "long"
        elif ofi <= -threshold:
            ofi_candidate = "short"
        else:
            # OFI не достигает порога ни в одну сторону — сбрасываем счётчик
            self._ofi_entry_confirmations = 0
            self._ofi_entry_direction = None
            return None

        if self._ofi_entry_direction == ofi_candidate:
            self._ofi_entry_confirmations += 1
        else:
            self._ofi_entry_confirmations = 1
            self._ofi_entry_direction = ofi_candidate

        if self._ofi_entry_confirmations < min_entry_conf:
            logger.debug(
                f"OFI {ofi_candidate.upper()} подтверждений: "
                f"{self._ofi_entry_confirmations}/{min_entry_conf}"
            )
            return None

        # ── Условие LONG ──────────────────────────────────────────────────
        if ofi_candidate == "long" and last_print.side == "buy":
            if trend_ma is not None and self._current_mid <= trend_ma:
                logger.debug(
                    f"LONG заблокирован фильтром тренда: "
                    f"mid={self._current_mid:.4f} <= MA({self._trend_ma_window})={trend_ma:.4f}"
                )
                return None
            logger.info(
                f"LONG сигнал: OFI={ofi:.3f} >= {threshold} "
                f"({self._ofi_entry_confirmations} подтверждений), "
                f"принт buy x{last_print.multiplier} @ {last_print.price:.2f}"
                + (f", mid={self._current_mid:.4f} > MA={trend_ma:.4f}" if trend_ma else "")
            )
            self._last_signal_time = timestamp
            self._ofi_entry_confirmations = 0
            self._ofi_entry_direction = None
            self.print_detector.clear_last_print()
            return Signal(
                signal_type=SignalType.LONG,
                reason=SignalReason.COMBO_TRIGGERED,
                ofi_value=ofi,
                print_volume=last_print.volume,
                print_side=last_print.side,
                timestamp=timestamp,
            )

        # ── Условие SHORT ─────────────────────────────────────────────────
        if ofi_candidate == "short" and last_print.side == "sell":
            if trend_ma is not None and self._current_mid >= trend_ma:
                logger.debug(
                    f"SHORT заблокирован фильтром тренда: "
                    f"mid={self._current_mid:.4f} >= MA({self._trend_ma_window})={trend_ma:.4f}"
                )
                return None
            logger.info(
                f"SHORT сигнал: OFI={ofi:.3f} <= -{threshold} "
                f"({self._ofi_entry_confirmations} подтверждений), "
                f"принт sell x{last_print.multiplier} @ {last_print.price:.2f}"
                + (f", mid={self._current_mid:.4f} < MA={trend_ma:.4f}" if trend_ma else "")
            )
            self._last_signal_time = timestamp
            self._ofi_entry_confirmations = 0
            self._ofi_entry_direction = None
            self.print_detector.clear_last_print()
            return Signal(
                signal_type=SignalType.SHORT,
                reason=SignalReason.COMBO_TRIGGERED,
                ofi_value=ofi,
                print_volume=last_print.volume,
                print_side=last_print.side,
                timestamp=timestamp,
            )

        return None

    def _is_trading_hours(self, timestamp: datetime) -> bool:
        """
        Проверить, находимся ли мы в разрешённых торговых часах.

        Торговые часы из конфига задаются по московскому времени.
        T-Invest API отдаёт UTC, поэтому нужно конвертировать.
        Разница UTC-MSK = +3 часа.
        """
        # Конвертируем UTC в московское время
        moscow_time = timestamp + timedelta(hours=3)
        current_time = moscow_time.time()

        if not (self._trading_start <= current_time <= self._trading_end):
            return False

        # Пропускаем первые N минут после открытия — высокая волатильность
        # на открытии делает сигналы менее надёжными
        skip_minutes = self.params.get("skip_first_minutes", 5)
        skip_until = (datetime(2000, 1, 1, self._trading_start.hour, self._trading_start.minute) + timedelta(minutes=skip_minutes)).time()

        if current_time < skip_until:
            return False

        return True

    def _is_cooldown_passed(self, timestamp: datetime) -> bool:
        """
        Проверить, прошёл ли cooldown.
        Два независимых кулдауна:
          1. cooldown_seconds — от последнего входа (предотвращает частые входы)
          2. post_close_cooldown_seconds — от последнего закрытия (предотвращает мгновенный флип)
        """
        if self._last_signal_time is not None:
            cooldown_seconds = self.params.get("cooldown_seconds", 60)
            if (timestamp - self._last_signal_time).total_seconds() < cooldown_seconds:
                return False

        if self._last_close_time is not None:
            post_close_cooldown = self.params.get("post_close_cooldown_seconds", 0)
            if post_close_cooldown > 0:
                if (timestamp - self._last_close_time).total_seconds() < post_close_cooldown:
                    return False

        return True

    def get_signal(self) -> Optional[Signal]:
        """
        Вернуть накопленный сигнал и сбросить его.
        Вызывается из основного цикла бота после каждого события.
        """
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    def set_position(self, direction: Optional[str], close_time: Optional[datetime] = None) -> None:
        """
        Сообщить стратегии о текущей позиции.
        Вызывается из position_manager при открытии/закрытии позиции.

        direction: None / "long" / "short"
        close_time: время закрытия позиции (передаётся при direction=None)
        """
        prev_direction = self._open_position_direction
        self._open_position_direction = direction
        # Сбрасываем счётчик подтверждений входа при любом изменении позиции.
        self._ofi_entry_confirmations = 0
        self._ofi_entry_direction = None
        # Запоминаем время закрытия для post_close_cooldown
        if prev_direction is not None and direction is None:
            self._last_close_time = close_time or datetime.utcnow()
        logger.debug(f"Стратегия: позиция обновлена → {direction}")

    @property
    def current_ofi(self) -> Optional[float]:
        """Текущее значение OFI (для мониторинга)."""
        return self._current_ofi

    def reset(self) -> None:
        """Полный сброс состояния стратегии."""
        self.ofi_calc.reset()
        self.print_detector.reset()
        self._pending_signal = None
        self._open_position_direction = None
        self._last_signal_time = None
        self._current_ofi = None
        self._ofi_entry_confirmations = 0
        self._ofi_entry_direction = None
        self._last_close_time = None
        if self._mid_history is not None:
            self._mid_history.clear()
        self._current_mid = None
