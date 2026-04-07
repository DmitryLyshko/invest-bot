"""Smoke-тест после правок."""
from trading_bot.core.strategy.combo_strategy import ComboStrategy
from trading_bot.core.strategy.base_strategy import SignalReason, SignalType
from trading_bot.core.execution.position_manager import PositionManager
from datetime import datetime

config = {
    "ofi_threshold": 0.75,
    "print_multiplier": 10.0,
    "print_window": 200,
    "ofi_levels": 5,
    "cooldown_seconds": 60,
    "ofi_smooth_window": 10,
    "min_hold_seconds": 30,
    "ofi_exit_threshold": 0.4,
    "min_ofi_confirmations": 3,
    "trading_hours": {"start": "10:05", "end": "18:30"},
    "skip_first_minutes": 5,
    "figi": "test",
    "lot_size": 10,
    "stop_ticks": 30,
    "take_profit_ticks": 120,
    "tick_size": 0.01,
}

# 1. SignalReason содержит TAKE_PROFIT
assert "take_profit" in [r.value for r in SignalReason], "TAKE_PROFIT отсутствует в SignalReason"
print("✓ SignalReason.TAKE_PROFIT есть")

# 2. Стратегия создаётся без ошибок
s = ComboStrategy(config)
print("✓ ComboStrategy создана")

# 3. skip_until не падает при start_m + skip_minutes >= 60
edge_config = dict(config, trading_hours={"start": "10:57", "end": "18:30"}, skip_first_minutes=5)
s2 = ComboStrategy(edge_config)
ts = datetime(2024, 1, 15, 8, 3)  # 11:03 MSK — внутри окна
s2._is_trading_hours(ts)
print("✓ skip_until не падает при переносе минут (10:57 + 5 мин)")

# 4. Тейк-профит срабатывает
from unittest.mock import MagicMock, patch

order_mock = MagicMock()
order_mock.price_executed = 300.00
order_mock.commission_rub = 1.5
order_mock.id = 1

order_manager = MagicMock()
order_manager.place_market_order.return_value = (order_mock, None)

strategy = MagicMock()
strategy.current_ofi = 0.8

pm = PositionManager(
    instrument_id=1,
    instrument_config=config,
    order_manager=order_manager,
    strategy=strategy,
)

# Симулируем открытую позицию
from trading_bot.core.execution.position_manager import OpenPosition
pm._position = OpenPosition(
    direction="long",
    entry_price=300.00,
    quantity_lots=1,
    open_at=datetime.utcnow(),
    open_order_id=1,
)

with patch("trading_bot.db.repository.save_signal") as mock_save, \
     patch("trading_bot.db.repository.save_trade"), \
     patch("trading_bot.db.repository.log_event"), \
     patch("trading_bot.db.repository.get_session") as mock_session:

    mock_save.return_value = MagicMock(id=2)
    mock_session.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock(return_value=None)))
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    # Тейк = 120 тиков * 0.01 = 1.20 руб. Цена входа 300.00, тейк при 301.20
    pm.update_market_price(301.25)  # чуть выше тейка

    assert pm._position is None, "Позиция должна быть закрыта по тейк-профиту"
    reason = mock_save.call_args_list[-1][1]["reason"]
    assert reason == "take_profit", f"Ожидали take_profit, получили: {reason}"

print("✓ Тейк-профит срабатывает корректно")

print("\nВсе проверки пройдены.")
