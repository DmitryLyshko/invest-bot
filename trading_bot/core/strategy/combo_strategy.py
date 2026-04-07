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

Логика выхода (Вариант 2 — разворот OFI):
  Выходим когда OFI развернулся против позиции:
  - Открыта LONG, OFI <= -threshold → продавцы перехватили инициативу → выходим
  - Открыта SHORT, OFI >= +threshold → покупатели перехватили → выходим

  Этот подход адаптивен: не фиксирует прибыль слишком рано (ждёт реального
  разворота потока), не держит убыточную позицию бесконечно (OFI разворачивается
  быстро при смене настроения).
"""
import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional

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

        # Создаём компоненты стратегии с параметрами из конфига
        self.ofi_calc = OFICalculator(ofi_levels=self.params["ofi_levels"])
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

        # Последнее рассчитанное OFI (для логгирования и отладки)
        self._current_ofi: Optional[float] = None

    def load_params(self, instrument_config: Dict[str, Any]) -> None:
        """Перезагрузить параметры и пересоздать компоненты."""
        super().load_params(instrument_config)
        # Если компоненты уже существуют — обновляем их параметры
        if hasattr(self, "ofi_calc"):
            self.ofi_calc = OFICalculator(ofi_levels=self.params["ofi_levels"])
        if hasattr(self, "print_detector"):
            self.print_detector = PrintDetector(
                print_window=self.params["print_window"],
                print_multiplier=self.params["print_multiplier"],
            )

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

        # Обновляем котировки для print_detector
        if bids and asks:
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            self.print_detector.update_quotes(best_bid, best_ask)

        # Пересчитываем OFI
        ofi = self.ofi_calc.update(bids, asks)
        if ofi is None:
            # Первый снапшот — нет предыдущего состояния для сравнения
            return

        self._current_ofi = ofi
        logger.debug(f"OFI={ofi:.3f} (порог={self.params['ofi_threshold']})")

        # Проверяем торговые часы
        if not self._is_trading_hours(timestamp):
            return

        # ── Логика выхода (приоритет над входом) ──────────────────────────
        if self._open_position_direction is not None:
            exit_signal = self._check_exit_condition(ofi, timestamp)
            if exit_signal is not None:
                self._pending_signal = exit_signal
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

        # Проверяем свежесть принта: он должен быть не старше 30 секунд
        # (устаревший принт уже не несёт актуальной информации)
        print_age = (timestamp - last_print.timestamp).total_seconds()
        if print_age > 30:
            return None

        # ── Условие LONG ──────────────────────────────────────────────────
        if ofi >= threshold and last_print.side == "buy":
            logger.info(
                f"LONG сигнал: OFI={ofi:.3f} >= {threshold}, "
                f"принт buy x{last_print.multiplier} @ {last_print.price:.2f}"
            )
            self._last_signal_time = timestamp
            # После использования сигнала сбрасываем принт
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
        if ofi <= -threshold and last_print.side == "sell":
            logger.info(
                f"SHORT сигнал: OFI={ofi:.3f} <= -{threshold}, "
                f"принт sell x{last_print.multiplier} @ {last_print.price:.2f}"
            )
            self._last_signal_time = timestamp
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

    def _check_exit_condition(self, ofi: float, timestamp: datetime) -> Optional[Signal]:
        """
        Проверить условие выхода из позиции.

        Стратегия выхода: OFI развернулся против нас.
        Это значит, что поток ордеров сменил направление — продолжать
        держать позицию рискованно.

        Порог выхода можно сделать мягче порога входа, чтобы не выходить
        на кратковременных колебаниях. Здесь используем threshold * 0.5
        (в два раза мягче), что даёт сделке "пространство для дыхания".
        """
        threshold = self.params["ofi_threshold"] * 0.5  # мягкий порог для выхода

        if self._open_position_direction == "long" and ofi <= -threshold:
            # OFI ушёл в минус при открытой лонг-позиции — продавцы усилились
            logger.info(f"EXIT LONG: OFI={ofi:.3f} развернулся (порог -{threshold:.3f})")
            return Signal(
                signal_type=SignalType.EXIT,
                reason=SignalReason.OFI_REVERSED,
                ofi_value=ofi,
                timestamp=timestamp,
            )

        if self._open_position_direction == "short" and ofi >= threshold:
            # OFI ушёл в плюс при открытой шорт-позиции — покупатели усилились
            logger.info(f"EXIT SHORT: OFI={ofi:.3f} развернулся (порог +{threshold:.3f})")
            return Signal(
                signal_type=SignalType.EXIT,
                reason=SignalReason.OFI_REVERSED,
                ofi_value=ofi,
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

        hours = self.params.get("trading_hours", {})
        start_str = hours.get("start", "10:05")
        end_str = hours.get("end", "18:30")

        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))

        start_time = time(start_h, start_m)
        end_time = time(end_h, end_m)

        if not (start_time <= current_time <= end_time):
            return False

        # Пропускаем первые N минут после открытия — высокая волатильность
        # на открытии делает сигналы менее надёжными
        skip_minutes = self.params.get("skip_first_minutes", 5)
        skip_until = time(start_h, start_m + skip_minutes) if start_m + skip_minutes < 60 else time(start_h + 1, (start_m + skip_minutes) % 60)

        if current_time < skip_until:
            return False

        return True

    def _is_cooldown_passed(self, timestamp: datetime) -> bool:
        """
        Проверить, прошёл ли cooldown с последнего сигнала.
        Cooldown предотвращает "перегрев" — слишком частые входы в рынок.
        """
        if self._last_signal_time is None:
            return True

        cooldown_seconds = self.params.get("cooldown_seconds", 60)
        elapsed = (timestamp - self._last_signal_time).total_seconds()
        return elapsed >= cooldown_seconds

    def get_signal(self) -> Optional[Signal]:
        """
        Вернуть накопленный сигнал и сбросить его.
        Вызывается из основного цикла бота после каждого события.
        """
        signal = self._pending_signal
        self._pending_signal = None
        return signal

    def set_position(self, direction: Optional[str]) -> None:
        """
        Сообщить стратегии о текущей позиции.
        Вызывается из position_manager при открытии/закрытии позиции.

        direction: None / "long" / "short"
        """
        self._open_position_direction = direction
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
