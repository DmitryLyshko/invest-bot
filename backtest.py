"""
Бэктест на исторических данных стакана и сделок из БД.

Использование:
  python backtest.py --ticker SBER --date 2026-04-08
  python backtest.py --ticker SBER --date-from 2026-04-07 --date-to 2026-04-08
  python backtest.py --ticker SBER --date 2026-04-08 --ofi-threshold 0.5 --stop-ticks 20

Параметры стратегии берутся из instruments.yaml по умолчанию.
Любой из них можно переопределить через аргументы командной строки.
"""
import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import yaml

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent))

from trading_bot.db import repository
from trading_bot.core.strategy.combo_strategy import ComboStrategy
from trading_bot.core.strategy.base_strategy import Signal, SignalType, SignalReason


# ─── Симулятор позиции ────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    direction: str
    entry_price: float
    exit_price: float
    quantity_lots: int
    lot_size: int
    open_at: datetime
    close_at: datetime
    exit_reason: str
    commission_rub: float

    @property
    def pnl_gross(self) -> float:
        if self.direction == "long":
            return (self.exit_price - self.entry_price) * self.quantity_lots * self.lot_size
        return (self.entry_price - self.exit_price) * self.quantity_lots * self.lot_size

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.commission_rub

    @property
    def hold_seconds(self) -> int:
        return int((self.close_at - self.open_at).total_seconds())


@dataclass
class BacktestPosition:
    direction: str
    entry_price: float
    quantity_lots: int
    open_at: datetime
    current_price: float = 0.0


class BacktestPositionManager:
    """
    Симулятор позиции для бэктеста — без реальных ордеров и БД.
    Повторяет логику PositionManager: стоп, тейк, тайм-аут.
    """

    def __init__(self, config: dict, commission_rate: float = 0.0005) -> None:
        self.config = config
        self.commission_rate = commission_rate  # 0.05% per side = 0.1% round trip
        self._position: Optional[BacktestPosition] = None
        self.trades: List[BacktestTrade] = []
        self._strategy: Optional[ComboStrategy] = None

    def set_strategy(self, strategy: ComboStrategy) -> None:
        self._strategy = strategy

    def on_signal(self, signal: Signal, current_price: float) -> None:
        if signal.signal_type in (SignalType.LONG, SignalType.SHORT):
            if self._position is not None:
                return  # нет пирамидинга
            direction = "long" if signal.signal_type == SignalType.LONG else "short"
            self._position = BacktestPosition(
                direction=direction,
                entry_price=current_price,
                quantity_lots=self.config.get("max_position_lots", 1),
                open_at=signal.timestamp,
                current_price=current_price,
            )
            if self._strategy:
                self._strategy.set_position(direction)

        elif signal.signal_type == SignalType.EXIT:
            if self._position is None:
                return
            # Проверяем min_hold_seconds для выхода по OFI
            if signal.reason == SignalReason.OFI_REVERSED:
                min_hold = self.config.get("min_hold_seconds", 0)
                held = (signal.timestamp - self._position.open_at).total_seconds()
                if held < min_hold:
                    return
            self._close(current_price, signal.timestamp, signal.reason.value)

    def update_market_price(self, price: float, timestamp: datetime) -> None:
        if self._position is None:
            return

        self._position.current_price = price
        tick_size = self.config.get("tick_size", 0.01)
        pos = self._position

        if pos.direction == "long":
            loss_distance = pos.entry_price - price
            gain_distance = price - pos.entry_price
        else:
            loss_distance = price - pos.entry_price
            gain_distance = pos.entry_price - price

        # Стоп-лосс
        stop_distance = self.config.get("stop_ticks", 30) * tick_size
        if loss_distance >= stop_distance:
            self._close(price, timestamp, "stop_loss")
            return

        # Тейк-профит
        tp_ticks = self.config.get("take_profit_ticks", 0)
        if tp_ticks > 0 and gain_distance >= tp_ticks * tick_size:
            self._close(price, timestamp, "take_profit")

    def check_timeout(self, timestamp: datetime) -> None:
        if self._position is None:
            return
        max_hold = self.config.get("max_hold_minutes", 60) * 60
        if (timestamp - self._position.open_at).total_seconds() >= max_hold:
            self._close(self._position.current_price, timestamp, "timeout")

    def _close(self, exit_price: float, close_at: datetime, reason: str) -> None:
        pos = self._position
        lot_size = self.config.get("lot_size", 1)
        position_value = exit_price * pos.quantity_lots * lot_size
        commission = position_value * self.commission_rate * 2  # вход + выход

        trade = BacktestTrade(
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity_lots=pos.quantity_lots,
            lot_size=lot_size,
            open_at=pos.open_at,
            close_at=close_at,
            exit_reason=reason,
            commission_rub=round(commission, 4),
        )
        self.trades.append(trade)
        self._position = None
        if self._strategy:
            self._strategy.set_position(None)

    @property
    def has_position(self) -> bool:
        return self._position is not None


# ─── Бэктест ──────────────────────────────────────────────────────────────────

def run_backtest(config: dict, figi: str, date_from: datetime, date_to: datetime, commission_rate: float):
    print(f"\nЗагрузка данных {figi} с {date_from.date()} по {(date_to - timedelta(seconds=1)).date()}...")

    orderbooks = repository.get_orderbook_snapshots(figi, date_from, date_to)
    trades_raw = repository.get_trade_ticks(figi, date_from, date_to)

    if not orderbooks:
        print("Нет данных стакана за указанный период.")
        print("Убедитесь что бот работал с RECORD_MARKET_DATA=true в этот день.")
        return

    print(f"Загружено: {len(orderbooks)} снапшотов стакана, {len(trades_raw)} тиков сделок")

    # Объединяем события в единый таймлайн.
    # JSON bids/asks парсим здесь один раз, а не 111К раз внутри цикла.
    events = []
    for ob in orderbooks:
        events.append(("ob", ob.recorded_at, json.loads(ob.bids), json.loads(ob.asks)))
    for t in trades_raw:
        events.append(("trade", t.recorded_at, t))
    events.sort(key=lambda x: x[1])

    # Создаём стратегию и симулятор
    strategy = ComboStrategy(config)
    pm = BacktestPositionManager(config, commission_rate=commission_rate)
    pm.set_strategy(strategy)

    last_price = 0.0
    last_timeout_check = date_from

    for event in events:
        event_type, ts = event[0], event[1]
        # Проверяем тайм-аут раз в минуту
        if (ts - last_timeout_check).total_seconds() >= 60:
            pm.check_timeout(ts)
            last_timeout_check = ts

        if event_type == "ob":
            bids, asks = event[2], event[3]
            ob_data = {"figi": figi, "bids": bids, "asks": asks, "time": ts}
            strategy.on_orderbook(ob_data)
            sig = strategy.get_signal()
            if sig is not None:
                pm.on_signal(sig, last_price)

        elif event_type == "trade":
            data = event[2]
            trade_data = {
                "figi": figi,
                "price": data.price,
                "quantity": data.quantity,
                "direction": data.direction,
                "time": data.recorded_at,
            }
            last_price = data.price
            strategy.on_trade(trade_data)
            pm.update_market_price(data.price, ts)
            sig = strategy.get_signal()
            if sig is not None:
                pm.on_signal(sig, last_price)

    # Закрываем незакрытую позицию по последней цене
    if pm.has_position:
        pm._close(last_price, events[-1][1], "end_of_data")

    return pm.trades


def print_results(trades: List[BacktestTrade], config: dict) -> None:
    if not trades:
        print("\nСделок нет. Попробуй снизить ofi_threshold или print_multiplier.")
        return

    total = len(trades)
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    total_pnl = sum(t.pnl_net for t in trades)
    gross_profit = sum(t.pnl_net for t in wins)
    gross_loss = abs(sum(t.pnl_net for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_hold = sum(t.hold_seconds for t in trades) / total

    by_reason = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.pnl_net)

    print("\n" + "=" * 55)
    print("  РЕЗУЛЬТАТЫ БЭКТЕСТА")
    print("=" * 55)
    print(f"  Всего сделок:      {total}")
    print(f"  Прибыльных:        {len(wins)} ({len(wins)/total*100:.1f}%)")
    print(f"  Убыточных:         {len(losses)} ({len(losses)/total*100:.1f}%)")
    print(f"  Итого P&L:         {total_pnl:+.2f} руб.")
    print(f"  Profit Factor:     {profit_factor:.2f}")
    print(f"  Средняя сделка:    {total_pnl/total:+.2f} руб.")
    if wins:
        print(f"  Средний выигрыш:   {gross_profit/len(wins):+.2f} руб.")
    if losses:
        print(f"  Средний проигрыш:  {-gross_loss/len(losses):+.2f} руб.")
    print(f"  Лучшая сделка:     {max(t.pnl_net for t in trades):+.2f} руб.")
    print(f"  Худшая сделка:     {min(t.pnl_net for t in trades):+.2f} руб.")
    print(f"  Среднее удержание: {int(avg_hold)}с")
    print()
    print("  По причинам выхода:")
    for reason, pnls in sorted(by_reason.items()):
        print(f"    {reason:<20} {len(pnls):>3} сделок  P&L={sum(pnls):+.2f} руб.")
    print()
    print("  Параметры стратегии:")
    print(f"    ofi_threshold={config['ofi_threshold']}  print_multiplier={config['print_multiplier']}")
    print(f"    stop_ticks={config.get('stop_ticks',30)}  take_profit_ticks={config.get('take_profit_ticks',0)}")
    print(f"    min_ofi_confirmations={config.get('min_ofi_confirmations',1)}")
    print("=" * 55)

    print("\n  Детали сделок:")
    print(f"  {'Открытие':<20} {'Закр.':<20} {'Напр.':<6} {'Вход':>7} {'Выход':>7} {'P&L':>8}  Причина")
    print("  " + "-" * 85)
    for t in trades:
        print(
            f"  {t.open_at.strftime('%m-%d %H:%M:%S'):<20} "
            f"{t.close_at.strftime('%m-%d %H:%M:%S'):<20} "
            f"{t.direction:<6} "
            f"{t.entry_price:>7.2f} "
            f"{t.exit_price:>7.2f} "
            f"{t.pnl_net:>+8.2f}  "
            f"{t.exit_reason}"
        )


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Бэктест торгового бота")
    parser.add_argument("--ticker", default="SBER", help="Тикер инструмента")
    parser.add_argument("--date", help="Дата бэктеста (YYYY-MM-DD)")
    parser.add_argument("--date-from", help="Начало периода (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="Конец периода включительно (YYYY-MM-DD)")
    parser.add_argument("--ofi-threshold", type=float, help="Порог OFI")
    parser.add_argument("--print-multiplier", type=float, help="Мультипликатор принта")
    parser.add_argument("--stop-ticks", type=int, help="Стоп-лосс в тиках")
    parser.add_argument("--take-profit-ticks", type=int, help="Тейк-профит в тиках")
    parser.add_argument("--min-ofi-confirmations", type=int, help="Подтверждений OFI для выхода")
    parser.add_argument("--commission", type=float, default=0.0005,
                        help="Комиссия за сторону (default: 0.0005 = 0.05%%)")
    parser.add_argument("--list-dates", action="store_true", help="Показать даты с данными")
    args = parser.parse_args()

    # Загружаем конфиг инструмента
    config_path = Path(__file__).parent / "trading_bot" / "config" / "instruments.yaml"
    with open(config_path) as f:
        all_config = yaml.safe_load(f)

    if args.ticker not in all_config:
        print(f"Тикер {args.ticker} не найден в instruments.yaml")
        sys.exit(1)

    config = dict(all_config[args.ticker])

    # Переопределяем параметры из CLI
    if args.ofi_threshold is not None:
        config["ofi_threshold"] = args.ofi_threshold
    if args.print_multiplier is not None:
        config["print_multiplier"] = args.print_multiplier
    if args.stop_ticks is not None:
        config["stop_ticks"] = args.stop_ticks
    if args.take_profit_ticks is not None:
        config["take_profit_ticks"] = args.take_profit_ticks
    if args.min_ofi_confirmations is not None:
        config["min_ofi_confirmations"] = args.min_ofi_confirmations

    figi = config["figi"]

    # Показать список дат с данными
    if args.list_dates:
        dates = repository.get_recorded_dates(figi)
        if dates:
            print(f"Данные есть за: {', '.join(dates)}")
        else:
            print("Данных нет. Запусти бота с RECORD_MARKET_DATA=true.")
        return

    # Определяем период
    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d")
        date_from = d
        date_to = d + timedelta(days=1)
    elif args.date_from and args.date_to:
        date_from = datetime.strptime(args.date_from, "%Y-%m-%d")
        date_to = datetime.strptime(args.date_to, "%Y-%m-%d") + timedelta(days=1)
    else:
        parser.print_help()
        print("\nУкажи --date или --date-from и --date-to")
        sys.exit(1)

    trades = run_backtest(config, figi, date_from, date_to, args.commission)
    if trades is not None:
        print_results(trades, config)


if __name__ == "__main__":
    main()
