# ARCHITECTURE.md

Справочник структуры проекта для быстрой навигации при правках.
Читай этот файл вместо сканирования всех исходников.

---

## Корневая структура

```
invest-bot/
├── trading_bot/          # Основной пакет
│   ├── config/           # Настройки и конфиг инструментов
│   ├── core/             # Бизнес-логика (стратегия, исполнение, данные, риск)
│   ├── db/               # Модели и репозиторий
│   ├── web/              # Flask дашборд
│   └── main.py           # Точка входа
├── requirements.txt
├── .env.example
├── README.md
└── ARCHITECTURE.md       # этот файл
```

---

## Поток данных (runtime)

```
T-Invest WebSocket
  └─▶ StreamHandler._run_stream()
        ├─▶ normalize_orderbook() / normalize_trade()   [market_data.py]
        ├─▶ ComboStrategy.on_orderbook()
        │     ├─▶ OFICalculator.update(bids, asks) → float [-1..1]
        │     ├─▶ PrintDetector.update_quotes(bid, ask)
        │     └─▶ _check_entry / _check_exit → Signal | None
        ├─▶ ComboStrategy.on_trade()
        │     └─▶ PrintDetector.on_trade() → PrintEvent | None
        └─▶ strategy.get_signal()
              └─▶ PositionManager.on_signal(signal)
                    ├─▶ RiskManager.check_all()   # блокирует → RiskCheckFailed
                    ├─▶ repository.save_signal()
                    └─▶ _open_position() / _close_position()
                          └─▶ OrderManager.place_market_order()
                                └─▶ T-Invest orders.post_order()
                                └─▶ repository.save_order / save_trade
```

Фоновые потоки в `main.py`:
- `stream_thread` (daemon) — стрим рыночных данных
- `web_thread` (daemon) — Flask дашборд
- `APScheduler` — `position_manager.check_timeout()` каждую минуту

---

## config/

### `settings.py`
Все переменные читаются из `.env`. Ключевые:
```python
TINKOFF_TOKEN: str
MYSQL_URL: str
WEB_SECRET_KEY: str
WEB_USERNAME: str / WEB_PASSWORD: str
DAILY_LOSS_LIMIT_RUB: float   # default -500.0
USE_SANDBOX: bool
LOG_DIR / LOG_FILE / LOG_LEVEL
INSTRUMENTS_CONFIG_PATH       # → trading_bot/config/instruments.yaml
```

### `instruments.yaml`
Один тикер = одна секция. Поля:
```yaml
SBER:
  figi, lot_size, ofi_threshold(0.6), print_multiplier(7.0),
  print_window(200), ofi_levels(5), cooldown_seconds(60),
  max_hold_minutes(60), stop_ticks(3), max_position_lots(1),
  trading_hours: {start, end}, skip_first_minutes(5)
```
Добавить тикер = добавить секцию сюда + перезапустить (в БД попадёт автоматически).

---

## core/strategy/

### `base_strategy.py`
```python
@dataclass Signal:
    signal_type: SignalType   # LONG | SHORT | EXIT
    reason: SignalReason      # COMBO_TRIGGERED | OFI_REVERSED | TIMEOUT | STOP_LOSS | MANUAL
    ofi_value, print_volume, print_side, timestamp

class BaseStrategy(ABC):
    on_orderbook(orderbook_data: dict)  # bids/asks: [(price, qty)]
    on_trade(trade_data: dict)          # price, quantity, direction, time
    get_signal() → Signal | None
    load_params(instrument_config: dict)
    reset()
```

### `ofi_calculator.py`
```python
class OFICalculator:
    __init__(ofi_levels: int)
    update(bids, asks) → float | None   # None на первом снапшоте
    reset()
    # property: last_ofi
```
Алгоритм: Cont-Kukanov-Stoikov по топ N уровней. Нормализация через `tanh(raw / 1000)`.
`_prev_bids/_prev_asks` хранят предыдущий снапшот для вычисления дельты.

### `print_detector.py`
```python
@dataclass PrintEvent: price, volume, side, multiplier, timestamp

class PrintDetector:
    __init__(print_window: int, print_multiplier: float)
    update_quotes(bid, ask)                    # вызывать перед on_trade
    on_trade(price, volume, direction, timestamp) → PrintEvent | None
    clear_last_print()
    reset()
    # properties: last_print, window_filled, current_median_volume
```
Медиана по `deque(maxlen=print_window)`. Агрессор: direction из API → fallback tick-test.
Минимальный прогрев: `max(10, print_window // 10)` точек.

### `combo_strategy.py`
```python
class ComboStrategy(BaseStrategy):
    __init__(instrument_config)
    on_orderbook(data)   # → обновляет OFI, проверяет вход/выход
    on_trade(data)       # → обновляет PrintDetector
    get_signal() → Signal | None   # сбрасывает после чтения
    set_position(direction: str | None)   # вызывать из PositionManager
    load_params(config)  # пересоздаёт OFICalculator и PrintDetector
    reset()
    # property: current_ofi
```
Вход: `|OFI| >= threshold` + принт той же стороны + свежесть принта ≤ 30с + cooldown.
Выход: `OFI` развернулся за порог `threshold * 0.5`.
Время UTC конвертируется в MSK (+3ч) для проверки `trading_hours`.

---

## core/execution/

### `order_manager.py`
```python
class OrderManager:
    __init__(account_id: str, instrument_id: int)
    place_market_order(figi, direction, quantity_lots, signal_id) → (Order, None) | (None, error_str)
    get_order_status(broker_order_id) → str | None
    cancel_order(broker_order_id) → bool
```
Запись в `orders` со статусом `"new"` создаётся ДО отправки в API (идемпотентность).
`client_order_id = uuid4()` — защита от дублей при сетевых ошибках.

### `position_manager.py`
```python
@dataclass OpenPosition:
    direction, entry_price, quantity_lots, open_at, open_order_id, signal_id, current_price
    # computed: unrealized_pnl, hold_seconds

class PositionManager:
    __init__(instrument_id, instrument_config, order_manager, strategy)
    on_signal(signal: Signal)           # главная точка входа
    update_market_price(price: float)   # вызывать при каждой сделке из стрима
    check_timeout()                     # вызывать из планировщика каждую минуту
    get_position_summary() → dict | None
    # properties: open_position, has_position
```
P&L: `(close - open) * lots * lot_size - commission`. Комиссия берётся из обоих ордеров.
Стоп-лосс: `stop_ticks * tick_size` (tick_size из конфига, дефолт 0.01).

---

## core/risk/

### `risk_manager.py`
```python
class RiskCheckFailed(Exception): ...

class RiskManager:
    __init__(instrument_id, instrument_config)
    check_all(signal_type, has_open_position, current_position_direction)
    # приватные: _check_bot_active, _check_trading_hours, _check_no_pyramiding, _check_daily_loss_limit
    _deny(reason, message)   # логирует в БД + бросает RiskCheckFailed
```
Порядок проверок: bot_active → trading_hours → пирамидинг → дневной лимит убытков.

---

## core/data/

### `stream_handler.py`
```python
class StreamHandler:
    __init__(figi, on_orderbook, on_trade, orderbook_depth=10)
    start()   # блокирующий, запускать в отдельном потоке; exponential backoff (1→60с)
    stop()
```
Подписки: `subscribe_order_book` + `subscribe_trades` в одном `create_market_data_stream()`.

### `market_data.py`
```python
normalize_orderbook(ob: OrderBook) → dict   # {figi, bids, asks, time}
normalize_trade(trade: TinkoffTrade) → dict  # {figi, price, quantity, direction, time}
get_spread(orderbook) → (bid, ask) | None
get_mid_price(orderbook) → float | None
```

---

## db/

### `models.py` — таблицы SQLAlchemy

| Таблица | Ключевые поля |
|---|---|
| `instruments` | ticker, figi, is_active, все параметры стратегии |
| `signals` | instrument_id, signal_type(long/short/exit), ofi_value, print_volume, print_side, reason, acted_on |
| `orders` | instrument_id, signal_id, order_id_broker, direction, quantity, price_executed, status(new/pending/filled/cancelled/rejected), commission_rub |
| `trades` | open/close_order_id, direction, open/close_price, pnl_rub, commission_rub, open/close_at, hold_seconds, exit_reason |
| `bot_logs` | level(INFO/WARNING/ERROR), component, message |
| `users` | username, password_hash, is_active, last_login |
| `bot_state` | id=1 (singleton), bot_active |

### `repository.py` — все функции

```python
# Инициализация
init_db()          # CREATE TABLE IF NOT EXISTS + создать BotState(id=1)
get_session()      # context manager, auto-rollback

# Instruments
get_instrument_by_ticker(ticker) / get_instrument_by_figi(figi)
get_active_instruments()
upsert_instrument(data: dict)

# Signals
save_signal(instrument_id, signal_type, ofi_value, print_volume, print_side, reason, acted_on) → Signal
mark_signal_acted(signal_id)
get_recent_signals(limit=50)
get_signals_page(page, per_page) → (List[Signal], total)

# Orders
save_order(instrument_id, signal_id, direction, quantity, ...) → Order
update_order_status(order_id, status, price_executed, commission_rub, order_id_broker)
get_order_by_broker_id(broker_id)

# Trades
save_trade(instrument_id, direction, open/close_price, quantity, pnl_rub, commission_rub, open/close_at, exit_reason, ...) → Trade
get_today_pnl(instrument_id=None) → float
get_trades_page(page, per_page, direction, exit_reason, date_from, date_to) → (List, total)
get_all_trades_for_export(...) → List[Trade]

# Aggregates (для веба)
get_stats_summary() → dict   # total_trades, win_rate, total_pnl, avg_win, avg_loss, profit_factor, best/worst, avg_hold
get_pnl_by_day(days=30) → List[{day, pnl}]
get_pnl_by_hour() → List[{hour, pnl, count}]
get_pnl_by_weekday() → List[{dow, pnl, count}]

# Logs
log_event(level, component, message)
get_recent_logs(limit=100)

# BotState
get_bot_active() → bool
set_bot_active(value: bool)

# Users
get_user_by_username(username) → User | None
create_user(username, password_hash) → User
update_last_login(user_id)
```

---

## web/

### `app.py`
```python
create_app() → Flask   # регистрирует blueprints + Flask-Login
get_position_manager() / set_position_manager(pm)   # инжекция из main.py
```
Blueprints: `dashboard`, `trades`, `signals`, `stats`.

### `auth.py`
```python
class WebUser(UserMixin): id, username
hash_password(password) → str          # bcrypt
check_password(password, hashed) → bool
load_user(user_id) → WebUser | None    # Flask-Login user_loader
authenticate(username, password) → WebUser | None
ensure_default_user(username, password)   # вызывается из main.py при старте
```

### `routes/dashboard.py`  blueprint=`dashboard`
```
GET  /                → dashboard.html  (KPI, recent signals/trades, equity chart)
POST /api/bot/toggle  → {active: bool}
GET  /api/bot/status  → {active: bool}
GET  /api/position    → {position: dict | null}
```

### `routes/trades.py`  blueprint=`trades`
```
GET /trades         → trades.html  (фильтры: direction, exit_reason, date_from, date_to)
GET /trades/export  → CSV-файл
```

### `routes/signals.py`  blueprints=`signals`, `stats`
```
GET /signals  → signals.html  (пагинация)
GET /stats    → stats.html    (aggregates + 3 Chart.js графика)
```

### `templates/`
```
base.html       — навигация, общие стили (тёмная тема, inline CSS)
login.html      — standalone (без base)
dashboard.html  — KPI карточки, equity chart, 2 таблицы
trades.html     — таблица с фильтрами + пагинация + CSV кнопка
signals.html    — таблица с пагинацией
stats.html      — 4+4 KPI карточки + 2 bar charts
error.html      — 404/500
```
Chart.js подключается через CDN (`cdn.jsdelivr.net/npm/chart.js@4.4.0`).

---

## main.py — последовательность старта

```python
setup_logging()              # RotatingFileHandler(logs/bot.log) + stdout
repository.init_db()         # CREATE TABLE + BotState singleton
ensure_default_user(...)     # создать admin если нет
config = load_instruments_config()      # читает instruments.yaml
instruments = sync_instruments_to_db()  # upsert в таблицу instruments
account_id = get_first_account_id()     # T-Invest accounts.get_accounts()[0]
strategy, order_manager, position_manager = build_components(ticker, params, account_id)
on_orderbook, on_trade = make_event_handlers(strategy, position_manager)
stream = StreamHandler(figi, on_orderbook, on_trade)
stream_thread.start()
scheduler.add_job(position_manager.check_timeout, "interval", minutes=1)
scheduler.start()
flask_app = create_app(); set_position_manager(position_manager)
web_thread.start()
signal.signal(SIGINT/SIGTERM, shutdown)   # graceful stop
stream_thread.join()   # main thread blocks here
```

---

## Соглашения и важные детали

| Что | Где | Детали |
|---|---|---|
| Конфиг → код | всегда через `instrument_config` dict | никаких глобалов в стратегиях |
| Добавить тикер | только `instruments.yaml` | код не трогать |
| Часовой пояс | UTC везде в БД | MSK = UTC+3, конвертация в `combo_strategy._is_trading_hours` и `risk_manager._check_trading_hours` |
| Порог выхода | `ofi_threshold * 0.5` | в `combo_strategy._check_exit_condition` |
| Принт "свежесть" | ≤ 30 секунд | в `combo_strategy._check_entry_condition` |
| Идемпотентность ордеров | `uuid4()` как `order_id` | `orders` запись создаётся ДО вызова API |
| Только одна позиция | контролируется `RiskManager._check_no_pyramiding` | |
| bot_active флаг | `bot_state.bot_active` в БД | переключается через `POST /api/bot/toggle` |
| Дневной лимит убытков | `DAILY_LOSS_LIMIT_RUB` из `.env` | `repository.get_today_pnl()` |
