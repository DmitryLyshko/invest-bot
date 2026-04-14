"""
Менеджер позиций.

Хранит состояние текущей позиции и оркестрирует:
  - открытие позиции (через order_manager)
  - мониторинг стоп-лосса и тайм-аута
  - закрытие позиции и расчёт P&L
  - запись сделки в БД

Ключевой принцип: только ОДНА позиция на инструмент одновременно.
Это упрощает управление рисками и логику P&L.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from trading_bot.core.execution.order_manager import OrderManager
from trading_bot.core.execution.portfolio_manager import PortfolioManager
from trading_bot.core.risk.risk_manager import RiskCheckFailed, RiskManager
from trading_bot.core.strategy.base_strategy import Signal, SignalReason, SignalType
from trading_bot.db import repository
from trading_bot.db.models import Order
from trading_bot.notifications.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    """Данные об открытой позиции."""
    direction: str         # "long" / "short"
    entry_price: float
    quantity_lots: int
    open_at: datetime
    open_order_id: int     # id записи в таблице orders
    signal_id: Optional[int] = None
    # Текущая рыночная цена (обновляется из стрима)
    current_price: float = 0.0
    # Флаг: стоп перенесён на цену входа (безубыток активирован)
    stop_at_breakeven: bool = False
    # Лучшая цена за время жизни позиции (для трейлинг-стопа)
    peak_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        """
        Нереализованный P&L с учётом направления.
        Для точного расчёта нужен lot_size из конфига инструмента —
        добавляется через compute_unrealized_pnl().
        """
        if self.direction == "long":
            return (self.current_price - self.entry_price) * self.quantity_lots
        else:
            return (self.entry_price - self.current_price) * self.quantity_lots

    @property
    def hold_seconds(self) -> int:
        return int((datetime.utcnow() - self.open_at).total_seconds())


class PositionManager:
    """
    Управляет единственной открытой позицией на инструмент.

    Оркестрирует:
      - risk_manager для проверки перед ордером
      - order_manager для выставления ордеров
      - combo_strategy для обновления состояния позиции
    """

    def __init__(
        self,
        instrument_id: int,
        instrument_config: Dict[str, Any],
        order_manager: OrderManager,
        strategy,  # ComboStrategy (не типизируем для избежания циклических импортов)
        portfolio_manager: Optional[PortfolioManager] = None,
        ticker: str = "",
        notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        self.instrument_id = instrument_id
        self.params = instrument_config
        self.order_manager = order_manager
        self.strategy = strategy
        self.portfolio_manager = portfolio_manager
        self.ticker = ticker
        self.notifier = notifier
        self.risk_manager = RiskManager(instrument_id, instrument_config, portfolio_manager)

        # Текущая открытая позиция (None если нет)
        self._position: Optional[OpenPosition] = None
        # Последняя известная рыночная цена (для расчёта размера лота)
        self._last_price: float = 0.0

    def on_signal(self, signal: Signal) -> None:
        """
        Обработать сигнал от стратегии.

        Это главная точка входа: здесь принимается решение о реальном ордере.
        """
        logger.info(f"Получен сигнал: {signal}")

        try:
            self.risk_manager.check_all(
                signal_type=signal.signal_type.value,
                has_open_position=self._position is not None,
                current_position_direction=self._position.direction if self._position else None,
            )
        except RiskCheckFailed as e:
            # Риск-менеджер отказал — сохраняем сигнал как "не исполненный"
            logger.warning(f"Риск-менеджер: {e}")
            repository.save_signal(
                instrument_id=self.instrument_id,
                signal_type=signal.signal_type.value,
                ofi_value=signal.ofi_value or 0.0,
                print_volume=signal.print_volume,
                print_side=signal.print_side,
                reason=signal.reason.value,
                acted_on=False,
            )
            return

        if signal.signal_type in (SignalType.LONG, SignalType.SHORT):
            # Глобальный лимит одновременных позиций — проверяем ДО записи сигнала.
            # Иначе сигнал попадёт в БД как acted_on=True, хотя ордер не выставлялся.
            if self.portfolio_manager and not self.portfolio_manager.can_open():
                count = self.portfolio_manager.open_positions_count
                max_pos = self.portfolio_manager._max_positions
                logger.warning(
                    f"Глобальный лимит позиций достигнут: {count}/{max_pos}. "
                    "Открытие заблокировано."
                )
                repository.save_signal(
                    instrument_id=self.instrument_id,
                    signal_type=signal.signal_type.value,
                    ofi_value=signal.ofi_value or 0.0,
                    print_volume=signal.print_volume,
                    print_side=signal.print_side,
                    reason=signal.reason.value,
                    acted_on=False,
                )
                repository.log_event(
                    "WARNING",
                    "position_manager",
                    f"Открытие заблокировано: достигнут лимит {max_pos} одновременных позиций",
                )
                return

            db_signal = repository.save_signal(
                instrument_id=self.instrument_id,
                signal_type=signal.signal_type.value,
                ofi_value=signal.ofi_value or 0.0,
                print_volume=signal.print_volume,
                print_side=signal.print_side,
                reason=signal.reason.value,
                acted_on=True,
            )
            self._open_position(signal, db_signal.id)
        elif signal.signal_type == SignalType.EXIT:
            # Проверяем минимальное время удержания позиции перед закрытием по OFI.
            # Это защита от мгновенного закрытия сразу после открытия из-за шума стакана.
            # Стоп-лосс и тайм-аут этот блок не затрагивают — они идут напрямую через
            # _close_position и не проходят через on_signal с reason=ofi_reversed.
            if signal.reason == SignalReason.OFI_REVERSED and self._position is not None:
                min_hold = self.params.get("min_hold_seconds", 0)
                held_seconds = (datetime.utcnow() - self._position.open_at).total_seconds()
                if held_seconds < min_hold:
                    logger.info(
                        f"Выход по OFI заблокирован: удержание {held_seconds:.1f} сек "
                        f"< минимум {min_hold} сек. Сигнал проигнорирован."
                    )
                    return
                # Не выходить по OFI если прибыль мала — после комиссии будет убыток.
                # При убытке (profit_ticks ≤ 0) выход разрешён: OFI подтверждает ошибку входа.
                min_profit_ticks = self.params.get("min_profit_ticks_for_ofi_exit", 0)
                if min_profit_ticks > 0 and self._position.current_price > 0:
                    tick_size = self.params.get("tick_size", 0.01)
                    pos = self._position
                    if pos.direction == "long":
                        profit_ticks = (pos.current_price - pos.entry_price) / tick_size
                    else:
                        profit_ticks = (pos.entry_price - pos.current_price) / tick_size
                    if 0 < profit_ticks < min_profit_ticks:
                        logger.info(
                            f"Выход по OFI заблокирован: прибыль {profit_ticks:.1f} тиков "
                            f"< минимум {min_profit_ticks} тиков (не покрывает комиссию)."
                        )
                        return
            db_signal = repository.save_signal(
                instrument_id=self.instrument_id,
                signal_type=signal.signal_type.value,
                ofi_value=signal.ofi_value or 0.0,
                print_volume=signal.print_volume,
                print_side=signal.print_side,
                reason=signal.reason.value,
                acted_on=True,
            )
            self._close_position(signal, db_signal.id, exit_reason=signal.reason.value)

    def _open_position(self, signal: Signal, signal_id: int) -> None:
        """Открыть новую позицию по сигналу."""
        direction = "buy" if signal.signal_type == SignalType.LONG else "sell"
        figi = self.params["figi"]

        # Рассчитываем размер позиции
        max_lots_cap = self.params.get("max_position_lots", None)
        if self.portfolio_manager:
            lot_size = self.params.get("lot_size", 1)
            quantity_lots = self.portfolio_manager.compute_lots(
                figi=figi,
                lot_size=lot_size,
                stream_price=self._last_price,   # 0.0 если стрим ещё не прогрелся
                max_lots_cap=max_lots_cap,
            )
        else:
            quantity_lots = max_lots_cap if max_lots_cap else 1

        order, error = self.order_manager.place_market_order(
            figi=figi,
            direction=direction,
            quantity_lots=quantity_lots,
            signal_id=signal_id,
        )

        if error or order is None:
            logger.error(f"Не удалось открыть позицию: {error}")
            return

        # Ждём цену исполнения — для рыночного ордера она приходит сразу
        entry_price = order.price_executed or 0.0
        if entry_price == 0.0:
            logger.warning("Цена исполнения = 0, используем запрошенную цену")

        position_direction = "long" if signal.signal_type == SignalType.LONG else "short"
        self._position = OpenPosition(
            direction=position_direction,
            entry_price=entry_price,
            quantity_lots=quantity_lots,
            open_at=datetime.utcnow(),
            open_order_id=order.id,
            signal_id=signal_id,
            peak_price=entry_price,
        )

        # Сообщаем стратегии о новой позиции — она начнёт следить за выходом
        self.strategy.set_position(position_direction)

        # Регистрируем позицию в глобальном портфеле
        if self.portfolio_manager:
            self.portfolio_manager.register_opened(self.instrument_id)

        repository.log_event(
            "INFO",
            "position_manager",
            f"Открыта {position_direction.upper()} позиция: "
            f"{quantity_lots} лотов @ {entry_price:.2f}",
        )
        logger.info(
            f"Позиция открыта: {position_direction.upper()} "
            f"{quantity_lots} лотов @ {entry_price:.2f}"
        )

        if self.notifier:
            self.notifier.send_position_opened(
                ticker=self.ticker,
                direction=position_direction,
                entry_price=entry_price,
                quantity_lots=quantity_lots,
                lot_size=self.params.get("lot_size", 1),
            )

    def _close_position(self, signal: Signal, signal_id: int, exit_reason: str) -> None:
        """Закрыть текущую позицию."""
        if self._position is None:
            logger.warning("Попытка закрыть позицию при её отсутствии")
            return

        pos = self._position
        close_direction = "sell" if pos.direction == "long" else "buy"
        figi = self.params["figi"]

        order, error = self.order_manager.place_market_order(
            figi=figi,
            direction=close_direction,
            quantity_lots=pos.quantity_lots,
            signal_id=signal_id,
        )

        if error or order is None:
            logger.error(f"Не удалось закрыть позицию: {error}")
            return

        close_price = order.price_executed if order.price_executed is not None else pos.current_price
        close_at = datetime.utcnow()

        # Рассчитываем P&L с учётом размера лота
        lot_size = self.params.get("lot_size", 1)

        if pos.direction == "long":
            pnl = (close_price - pos.entry_price) * pos.quantity_lots * lot_size
        else:
            pnl = (pos.entry_price - close_price) * pos.quantity_lots * lot_size

        # Учитываем комиссии (открытие + закрытие)
        commission = (pos_open_commission := 0.0)
        # Берём из ордеров если доступно
        from trading_bot.db.repository import get_session
        from trading_bot.db.models import Order as OrderModel
        with get_session() as session:
            open_ord = session.get(OrderModel, pos.open_order_id)
            if open_ord and open_ord.commission_rub:
                commission += open_ord.commission_rub
        if order.commission_rub:
            commission += order.commission_rub

        pnl_after_commission = pnl - commission

        repository.log_event(
            "INFO",
            "position_manager",
            f"Закрыта {pos.direction.upper()} позиция: "
            f"{pos.quantity_lots} лотов @ {close_price:.2f}, "
            f"P&L={pnl_after_commission:.2f} руб., причина={exit_reason}",
        )
        logger.info(
            f"Позиция закрыта: {pos.direction.upper()} @ {close_price:.2f}, "
            f"P&L={pnl_after_commission:.2f} руб."
        )

        # Уведомление отправляем ДО записи в БД — чтобы ошибка сохранения
        # не заблокировала получение информации о результате сделки.
        if self.notifier:
            hold_secs = int((close_at - pos.open_at).total_seconds())
            self.notifier.send_position_closed(
                ticker=self.ticker,
                direction=pos.direction,
                entry_price=pos.entry_price,
                close_price=close_price,
                quantity_lots=pos.quantity_lots,
                lot_size=self.params.get("lot_size", 1),
                pnl=pnl_after_commission,
                hold_seconds=hold_secs,
                exit_reason=exit_reason,
            )

        # Записываем завершённую сделку в БД
        repository.save_trade(
            instrument_id=self.instrument_id,
            direction=pos.direction,
            open_price=pos.entry_price,
            close_price=close_price,
            quantity=pos.quantity_lots,
            pnl_rub=pnl_after_commission,
            commission_rub=commission,
            open_at=pos.open_at,
            close_at=close_at,
            exit_reason=exit_reason,
            open_order_id=pos.open_order_id,
            close_order_id=order.id,
        )

        # Сбрасываем позицию
        self._position = None
        self.strategy.set_position(None, close_time=close_at)

        # Снимаем регистрацию в глобальном портфеле
        if self.portfolio_manager:
            self.portfolio_manager.register_closed(self.instrument_id)

    def update_market_price(self, price: float) -> None:
        """
        Обновить текущую рыночную цену.
        Вызывается при каждой сделке из стрима.

        Используется для:
        - расчёта нереализованного P&L на дашборде
        - проверки стоп-лосса
        - расчёта размера лота при следующем открытии
        """
        self._last_price = price
        if self._position is None:
            return

        self._position.current_price = price
        self._check_stop_loss(price)

    def check_timeout(self) -> None:
        """
        Проверить истечение максимального времени удержания.
        Вызывается по расписанию (раз в минуту) из планировщика.
        """
        if self._position is None:
            return

        max_hold = self.params.get("max_hold_minutes", 60)
        if self._position.hold_seconds >= max_hold * 60:
            logger.info(
                f"Тайм-аут позиции: удержание {self._position.hold_seconds // 60} мин. "
                f">= лимит {max_hold} мин."
            )
            from trading_bot.core.strategy.base_strategy import Signal, SignalType, SignalReason
            timeout_signal = Signal(
                signal_type=SignalType.EXIT,
                reason=SignalReason.TIMEOUT,
            )
            # Сохраняем сигнал и закрываем позицию
            db_signal = repository.save_signal(
                instrument_id=self.instrument_id,
                signal_type="exit",
                ofi_value=self.strategy.current_ofi or 0.0,
                print_volume=None,
                print_side=None,
                reason="timeout",
                acted_on=True,
            )
            self._close_position(timeout_signal, db_signal.id, exit_reason="timeout")

    def _check_stop_loss(self, current_price: float) -> None:
        """
        Проверить стоп-лосс и тейк-профит в тиках.

        stop_ticks  — движение против позиции, при котором закрываемся с убытком.
        take_profit_ticks — движение в пользу позиции, при котором фиксируем прибыль.

        Размер тика зависит от инструмента. Для SBER ≈ 0.01 руб (1 копейка).
        В production нужно брать min_price_increment из API инструмента.
        """
        if self._position is None:
            return

        tick_size = self.params.get("tick_size", 0.01)
        stop_ticks = self.params.get("stop_ticks", 30)
        trailing_stop_ticks = self.params.get("trailing_stop_ticks", 0)
        pos = self._position

        from trading_bot.core.strategy.base_strategy import Signal, SignalType, SignalReason

        if pos.direction == "long":
            loss_distance = pos.entry_price - current_price
            gain_distance = current_price - pos.entry_price
        else:
            loss_distance = current_price - pos.entry_price
            gain_distance = pos.entry_price - current_price

        # ── Трейлинг-стоп (заменяет безубыток если включён) ─────────────────
        if trailing_stop_ticks > 0:
            trail_distance = trailing_stop_ticks * tick_size
            initial_stop_distance = stop_ticks * tick_size

            # Обновляем лучшую цену
            if pos.direction == "long":
                if current_price > pos.peak_price:
                    pos.peak_price = current_price
                trailing_stop_price = pos.peak_price - trail_distance
                initial_stop_price  = pos.entry_price - initial_stop_distance
                stop_price = max(initial_stop_price, trailing_stop_price)
                triggered = current_price <= stop_price
            else:
                if current_price < pos.peak_price:
                    pos.peak_price = current_price
                trailing_stop_price = pos.peak_price + trail_distance
                initial_stop_price  = pos.entry_price + initial_stop_distance
                stop_price = min(initial_stop_price, trailing_stop_price)
                triggered = current_price >= stop_price

            if triggered:
                gave_back = abs(pos.peak_price - current_price) / tick_size
                logger.info(
                    f"ТРЕЙЛИНГ-СТОП: пик={pos.peak_price:.4f}, "
                    f"стоп={stop_price:.4f}, цена={current_price:.4f} "
                    f"(откат {gave_back:.1f} тиков)"
                )
                db_signal = repository.save_signal(
                    instrument_id=self.instrument_id,
                    signal_type="exit",
                    ofi_value=self.strategy.current_ofi or 0.0,
                    print_volume=None,
                    print_side=None,
                    reason="trailing_stop",
                    acted_on=True,
                )
                self._close_position(
                    Signal(signal_type=SignalType.EXIT, reason=SignalReason.TRAILING_STOP),
                    db_signal.id,
                    exit_reason="trailing_stop",
                )
                return

        else:
            # ── Безубыток (классический режим) ───────────────────────────────
            breakeven_ticks = self.params.get("breakeven_ticks", 0)
            if breakeven_ticks > 0 and not pos.stop_at_breakeven:
                if gain_distance >= breakeven_ticks * tick_size:
                    pos.stop_at_breakeven = True
                    logger.info(
                        f"БЕЗУБЫТОК активирован: цена прошла {breakeven_ticks} тиков в пользу позиции"
                    )

            # ── Стоп-лосс ────────────────────────────────────────────────────
            if pos.stop_at_breakeven:
                if loss_distance > 0:
                    logger.info(f"БЕЗУБЫТОК сработал: цена вернулась за точку входа {pos.entry_price:.2f}")
                    db_signal = repository.save_signal(
                        instrument_id=self.instrument_id,
                        signal_type="exit",
                        ofi_value=self.strategy.current_ofi or 0.0,
                        print_volume=None,
                        print_side=None,
                        reason="breakeven_stop",
                        acted_on=True,
                    )
                    self._close_position(
                        Signal(signal_type=SignalType.EXIT, reason=SignalReason.STOP_LOSS),
                        db_signal.id,
                        exit_reason="breakeven_stop",
                    )
                    return
            else:
                stop_distance = stop_ticks * tick_size
                if loss_distance >= stop_distance:
                    logger.info(
                        f"СТОП-ЛОСС: движение против позиции {loss_distance:.4f} >= {stop_distance:.4f} "
                        f"({stop_ticks} тиков)"
                    )
                    db_signal = repository.save_signal(
                        instrument_id=self.instrument_id,
                        signal_type="exit",
                        ofi_value=self.strategy.current_ofi or 0.0,
                        print_volume=None,
                        print_side=None,
                        reason="stop_loss",
                        acted_on=True,
                    )
                    self._close_position(
                        Signal(signal_type=SignalType.EXIT, reason=SignalReason.STOP_LOSS),
                        db_signal.id,
                        exit_reason="stop_loss",
                    )
                    return

        # ── Тейк-профит ──────────────────────────────────────────────────────
        take_profit_ticks = self.params.get("take_profit_ticks", 0)
        if take_profit_ticks > 0:
            take_distance = take_profit_ticks * tick_size
            if gain_distance >= take_distance:
                logger.info(
                    f"ТЕЙК-ПРОФИТ: движение в пользу позиции {gain_distance:.4f} >= {take_distance:.4f} "
                    f"({take_profit_ticks} тиков)"
                )
                tp_signal = Signal(signal_type=SignalType.EXIT, reason=SignalReason.TAKE_PROFIT)
                db_signal = repository.save_signal(
                    instrument_id=self.instrument_id,
                    signal_type="exit",
                    ofi_value=self.strategy.current_ofi or 0.0,
                    print_volume=None,
                    print_side=None,
                    reason="take_profit",
                    acted_on=True,
                )
                self._close_position(tp_signal, db_signal.id, exit_reason="take_profit")

    @property
    def open_position(self) -> Optional[OpenPosition]:
        """Текущая открытая позиция (или None)."""
        return self._position

    @property
    def has_position(self) -> bool:
        return self._position is not None

    def get_position_summary(self) -> Optional[dict]:
        """Сводка по позиции для веб-дашборда."""
        if self._position is None:
            return None
        pos = self._position
        lot_size = self.params.get("lot_size", 1)
        return {
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "current_price": pos.current_price,
            "quantity_lots": pos.quantity_lots,
            "unrealized_pnl": round(pos.unrealized_pnl * lot_size, 2),
            "open_at": pos.open_at.isoformat(),
            "hold_minutes": pos.hold_seconds // 60,
        }
