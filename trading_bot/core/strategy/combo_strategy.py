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

        # Создаём компоненты стратегии с параметрами из конфига.
        # smooth_window — окно сглаживания OFI; если не задан в конфиге, используем 1
        # (отключено — поведение как раньше, для обратной совместимости).
        self.ofi_calc = OFICalculator(
            ofi_levels=self.params["ofi_levels"],
            smooth_window=self.params.get("ofi_smooth_window", 1),
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

        # Последнее рассчитанное OFI (для логгирования и отладки)
        self._current_ofi: Optional[float] = None

        # Счётчик последовательных подтверждений разворота OFI.
        # Увеличивается на каждый апдейт стакана, где OFI против позиции.
        # Сбрасывается, когда OFI перестаёт быть против позиции или при открытии
        # новой позиции. Выход разрешён только при достижении min_ofi_confirmations.
        self._ofi_exit_confirmations: int = 0

    def load_params(self, instrument_config: Dict[str, Any]) -> None:
        """Перезагрузить параметры и пересоздать компоненты."""
        super().load_params(instrument_config)
        # Если компоненты уже существуют — обновляем их параметры
        if hasattr(self, "ofi_calc"):
            self.ofi_calc = OFICalculator(
                ofi_levels=self.params["ofi_levels"],
                smooth_window=self.params.get("ofi_smooth_window", 1),
            )
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
        logger.debug(f"OFI={ofi:.3f}")

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

        # Проверяем свежесть принта: он должен быть не старше 15 секунд
        # (устаревший принт уже не несёт актуальной информации)
        print_age = (timestamp - last_print.timestamp).total_seconds()
        if print_age > 15:
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

        Три уровня защиты от преждевременного закрытия:

        1. Минимальное время удержания (min_hold_seconds):
           Позиция не может быть закрыта по OFI раньше этого порога.
           Защита от мгновенного флипа стакана сразу после входа.

        2. Порог OFI для выхода (ofi_exit_threshold):
           Отдельный порог, может быть ниже порога входа. OFI должен
           уверенно указывать против позиции, не просто быть отрицательным.

        3. Подтверждения подряд (min_ofi_confirmations):
           OFI должен быть против позиции на N последовательных апдейтах
           стакана. Один случайный флип не закрывает позицию.
        """
        # Порог выхода из конфига; по умолчанию — половина порога входа
        # (обратная совместимость со старыми конфигами без нового параметра)
        exit_threshold = self.params.get(
            "ofi_exit_threshold",
            self.params["ofi_threshold"] * 0.5,
        )

        # Проверяем, направлен ли OFI против текущей позиции
        ofi_is_against = self._is_ofi_against_position(ofi, exit_threshold)

        if not ofi_is_against:
            # OFI не против позиции — сбрасываем счётчик подтверждений.
            # Позиция может "выдохнуть" и восстановиться без накопленного счётчика.
            if self._ofi_exit_confirmations > 0:
                logger.debug(
                    f"OFI вернулся в пользу позиции ({ofi:.3f}), "
                    f"сброс счётчика подтверждений ({self._ofi_exit_confirmations} → 0)"
                )
            self._ofi_exit_confirmations = 0
            return None

        # OFI против позиции — увеличиваем счётчик подтверждений
        self._ofi_exit_confirmations += 1
        required_confirmations = self.params.get("min_ofi_confirmations", 1)

        logger.debug(
            f"OFI против позиции: {ofi:.3f}, подтверждений: "
            f"{self._ofi_exit_confirmations}/{required_confirmations}"
        )

        if self._ofi_exit_confirmations < required_confirmations:
            # Ещё не набрали нужное количество подтверждений — ждём
            return None

        # Достаточно подтверждений — генерируем сигнал выхода
        direction = self._open_position_direction
        logger.info(
            f"EXIT {direction.upper()}: OFI={ofi:.3f} против позиции "
            f"на {self._ofi_exit_confirmations} апдейтах подряд "
            f"(порог={exit_threshold:.3f}, требуется={required_confirmations})"
        )
        self._ofi_exit_confirmations = 0
        return Signal(
            signal_type=SignalType.EXIT,
            reason=SignalReason.OFI_REVERSED,
            ofi_value=ofi,
            timestamp=timestamp,
        )

    def _is_ofi_against_position(self, ofi: float, threshold: float) -> bool:
        """
        Проверить, направлен ли OFI против текущей открытой позиции.

        Для LONG позиции OFI "против" = значение ниже -threshold
        (продавцы перехватили инициативу).
        Для SHORT позиции OFI "против" = значение выше +threshold
        (покупатели перехватили инициативу).
        """
        if self._open_position_direction == "long":
            return ofi <= -threshold
        if self._open_position_direction == "short":
            return ofi >= threshold
        return False

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
        skip_until = (datetime(2000, 1, 1, start_h, start_m) + timedelta(minutes=skip_minutes)).time()

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
        # Сбрасываем счётчик подтверждений при любом изменении позиции.
        # При открытии новой позиции — старый счётчик неактуален.
        # При закрытии — тем более.
        self._ofi_exit_confirmations = 0
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
        self._ofi_exit_confirmations = 0
