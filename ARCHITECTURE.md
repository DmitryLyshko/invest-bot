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
T-Invest WebSocket (TINKOFF_MARKET_TOKEN)
  └─▶ StreamHandler._run_stream()
        ├─▶ normalize_orderbook() / normalize_trade()   [market_data.py]
        ├─▶ DataRecorder.on_orderbook() / on_trade()    [опционально, RECORD_MARKET_DATA]
        ├─▶ ComboStrategy.on_orderbook()
        │     ├─▶ OFICalculator.update(bids, asks) → float [-1..1]  (со сглаживанием)
        │     ├─▶ PrintDetector.update_quotes(bid, ask)
        │     └─▶ _check_exit_condition / _check_entry_condition → Signal | None
        │           └─▶ _ofi_exit_confirmations счётчик подтверждений
        ├─▶ ComboStrategy.on_trade()
        │     └─▶ PrintDetector.on_trade() → PrintEvent | None
        └─▶ strategy.get_signal()   # вызывается ПОСЛЕ on_orderbook И on_trade
              └─▶ PositionManager.on_signal(signal)
                    ├─▶ RiskManager.check_all()   # блокирует → RiskCheckFailed
                    ├─▶ repository.save_signal()
                    └─▶ _open_position() / _close_position()
                          └─▶ OrderManager.place_market_order()
                                └─▶ T-Invest orders.post_order() / sandbox.post_sandbox_order()
                                └─▶ repository.save_order / update_order_status / save_trade

PositionManager.update_market_price(price)   # вызывается при каждой сделке из стрима
  └─▶ _check_stop_loss(price)
        ├─▶ breakeven: если gain >= breakeven_ticks → активировать stop_at_breakeven
        ├─▶ стоп-лосс: loss >= stop_ticks (или цена вернулась за вход при безубытке)
        └─▶ тейк-профит: gain >= take_profit_ticks (если take_profit_ticks > 0)
```

Фоновые потоки в `main.py`:
- `stream_thread` (daemon) — стрим рыночных данных
- `web_thread` (daemon) — Flask дашборд
- `APScheduler` — `position_manager.check_timeout()` каждую минуту

**Мультитикер:** каждый тикер из `instruments.yaml` получает свой поток `stream_thread`, свои `strategy/order_manager/position_manager`, свой job в планировщике. Все изолированы — падение одного не влияет на другие.

---

## config/

### `settings.py`
Все переменные читаются из `.env`. Ключевые:
```python
TINKOFF_TOKEN: str                     # для ордеров и sandbox
TINKOFF_MARKET_TOKEN: str              # для маркет-дата стрима (fallback → TINKOFF_TOKEN)
MYSQL_URL: str
WEB_SECRET_KEY: str
WEB_USERNAME: str / WEB_PASSWORD: str
WEB_HOST: str                          # default "127.0.0.1"
WEB_PORT: int                          # default 5000
DAILY_LOSS_LIMIT_RUB: float            # default -500.0
USE_SANDBOX: bool                      # SANDBOX=true в .env
LOG_DIR / LOG_FILE / LOG_LEVEL
INSTRUMENTS_CONFIG_PATH                # → trading_bot/config/instruments.yaml
RECORD_MARKET_DATA: bool               # писать стаканы/тики в БД для бэктеста
RECORD_ORDERBOOK_INTERVAL: int         # каждый N-й снапшот стакана (default 1)
```

### `instruments.yaml`
Один тикер = одна секция. Все поля:
```yaml
SBER:
  figi, instrument_id         # instrument_id нужен для подписки стакана
  lot_size, commission_rate   # commission_rate для расчёта комиссии

  # Параметры стратегии
  ofi_threshold: 0.75         # порог OFI для входа
  ofi_levels: 5               # топ N уровней стакана
  ofi_smooth_window: 12       # окно сглаживания OFI (апдейтов стакана)
  ofi_exit_threshold: 0.5     # порог OFI для выхода (независимо от порога входа)
  min_ofi_confirmations: 4    # подтверждений подряд для выхода по OFI
  print_multiplier: 10.0      # объём >= медиана * multiplier → крупный принт
  print_window: 200           # размер окна медианы объёмов

  # Cooldown
  cooldown_seconds: 60        # между сигналами входа
  post_close_cooldown_seconds: 90  # после закрытия позиции (защита от флипа)

  # Управление позицией
  max_hold_minutes: 60
  min_hold_seconds: 120       # минимальное время до OFI-выхода
  min_profit_ticks_for_ofi_exit: 28  # OFI-выход блокируется при малой прибыли
  trend_ma_window: 300               # окно MA mid-цен для фильтра тренда (0 = выкл)
  max_position_lots: 1

  # Стопы и тейк
  tick_size: 0.01
  stop_ticks: 40
  breakeven_ticks: 35         # перенести стоп в безубыток после N тиков в плюс
  take_profit_ticks: 120      # 0 = отключён

  trading_hours: {start: "10:05", end: "18:30"}
  skip_first_minutes: 5
```
Добавить тикер = добавить секцию сюда + перезапустить (в БД попадёт автоматически).

---

## core/strategy/

### `base_strategy.py`
```python
@dataclass Signal:
    signal_type: SignalType   # LONG | SHORT | EXIT
    reason: SignalReason      # COMBO_TRIGGERED | OFI_REVERSED | TIMEOUT | STOP_LOSS | TAKE_PROFIT | MANUAL
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
    __init__(ofi_levels: int, smooth_window: int = 1)
    update(bids, asks) → float | None   # None на первом снапшоте
    reset()
    # property: last_ofi
```
Алгоритм: Cont-Kukanov-Stoikov по топ N уровней. Нормализация через `tanh(raw / 1000)`.
Сглаживание: скользящее среднее по `smooth_window` последних значений.
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
    set_position(direction: str | None, close_time: datetime | None)  # из PositionManager
    load_params(config)  # пересоздаёт OFICalculator и PrintDetector
    reset()
    # property: current_ofi
```
**Вход:** `|OFI| >= ofi_threshold` + принт той же стороны + свежесть принта ≤ **15с** + cooldown + фильтр тренда.
**Фильтр тренда:** если `trend_ma_window > 0` — LONG разрешён только при `mid > MA(N)`, SHORT — при `mid < MA(N)`. Окно накапливает mid-цены из стакана; до заполнения входы блокируются. `0` = отключён.
**Выход:** OFI против позиции на `min_ofi_confirmations` подтверждений подряд, `|OFI| >= ofi_exit_threshold`.
**Защита от преждевременного закрытия:**
- `_ofi_exit_confirmations` — счётчик сбрасывается если OFI перестаёт быть против позиции
- `min_hold_seconds` — блокировка в `PositionManager.on_signal`
- `min_profit_ticks_for_ofi_exit` — блокировка в `PositionManager.on_signal`

Два независимых кулдауна: `cooldown_seconds` (от последнего входа) + `post_close_cooldown_seconds` (от закрытия).
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
Поддерживает sandbox (`SandboxClient`) и prod (`Client`) автоматически через `USE_SANDBOX`.

### `position_manager.py`
```python
@dataclass OpenPosition:
    direction, entry_price, quantity_lots, open_at, open_order_id, signal_id, current_price
    stop_at_breakeven: bool    # флаг: безубыток активирован
    # computed: unrealized_pnl, hold_seconds

class PositionManager:
    __init__(instrument_id, instrument_config, order_manager, strategy)
    on_signal(signal: Signal)           # главная точка входа
    update_market_price(price: float)   # вызывать при каждой сделке → проверяет стоп/тейк
    check_timeout()                     # вызывать из планировщика каждую минуту
    get_position_summary() → dict | None
    # properties: open_position, has_position
```
**P&L:** `(close - open) * lots * lot_size - commission`. Комиссия суммируется из обоих ордеров.
**Стоп-лосс:** `stop_ticks * tick_size` (tick_size из конфига, дефолт 0.01).
**Безубыток:** после движения на `breakeven_ticks` в плюс — стоп переносится на цену входа (`stop_at_breakeven = True`). При возврате за цену входа — закрытие с `exit_reason="breakeven_stop"`.
**Тейк-профит:** если `take_profit_ticks > 0` и движение в плюс достигло порога — закрытие с `exit_reason="take_profit"`.

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
    __init__(figi, on_orderbook, on_trade, orderbook_depth=10, instrument_id="")
    start()   # блокирующий, запускать в отдельном потоке; exponential backoff (1→60с)
    stop()
```
Подписки: `subscribe_order_book` + `subscribe_trades` в одном `create_market_data_stream()`.
Использует `TINKOFF_MARKET_TOKEN` (отдельный токен для маркет-дата).
`instrument_id` передаётся в `OrderBookInstrument` для подписки на стакан.

### `market_data.py`
```python
normalize_orderbook(ob: OrderBook) → dict   # {figi, bids, asks, time}
normalize_trade(trade: TinkoffTrade) → dict  # {figi, price, quantity, direction, time}
get_spread(orderbook) → (bid, ask) | None
get_mid_price(orderbook) → float | None
```

### `data_recorder.py`
```python
class DataRecorder:
    __init__(figi: str, instrument_config: dict | None)   # активен если RECORD_MARKET_DATA=true
    on_orderbook(data: dict) → None   # запись стакана; каждый N-й (RECORD_ORDERBOOK_INTERVAL)
    on_trade(data: dict) → None       # запись тикового трейда
```
Подключается к тем же колбэкам что и стратегия — параллельно, без вмешательства.
При `RECORD_MARKET_DATA=false` — no-op. Запись ведётся только в `trading_hours` (MSK) из конфига инструмента; если часы не заданы — пишет всегда. SBER генерирует ~150-300k строк стакана/день в торговое время.

---

## db/

### `models.py` — таблицы SQLAlchemy

| Таблица | Ключевые поля |
|---|---|
| `instruments` | ticker, figi, is_active, все параметры стратегии (включая ofi_smooth_window, min_hold_seconds, ofi_exit_threshold, min_ofi_confirmations) |
| `signals` | instrument_id, signal_type(long/short/exit), ofi_value, print_volume, print_side, reason, acted_on |
| `orders` | instrument_id, signal_id, order_id_broker, direction, quantity, price_executed, status(new/pending/filled/cancelled/rejected), commission_rub |
| `trades` | open/close_order_id, direction, open/close_price, pnl_rub, commission_rub, open/close_at, hold_seconds, exit_reason |
| `bot_logs` | level(INFO/WARNING/ERROR), component, message |
| `users` | username, password_hash, is_active, last_login |
| `bot_state` | id=1 (singleton), bot_active |
| `market_orderbooks` | figi, bids(JSON), asks(JSON), recorded_at — индекс по (figi, recorded_at) |
| `market_trade_ticks` | figi, price, quantity, direction, recorded_at — индекс по (figi, recorded_at) |

`exit_reason` значения в `trades`: `ofi_reversed`, `timeout`, `stop_loss`, `breakeven_stop`, `take_profit`, `manual`.

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
get_stats_summary(instrument_id=None) → dict   # total_trades, win_rate, total_pnl, avg_win, avg_loss, profit_factor, best/worst, avg_hold
get_pnl_by_day(days=30) → List[{day, pnl}]
get_pnl_by_hour(instrument_id=None) → List[{hour, pnl, count}]
get_pnl_by_weekday(instrument_id=None) → List[{dow, pnl, count}]
# instrument_id=None → все тикеры; передать int → фильтр по инструменту

# Market data (бэктест)
save_orderbook_snapshot(figi, bids, asks, timestamp)
save_trade_tick(figi, price, quantity, direction, timestamp)

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
get_position_managers() → dict[ticker, PositionManager]   # инжекция из main.py
set_position_managers(pms: dict) → None
```
Blueprints: `dashboard`, `trades`, `signals`, `stats`, `instruments`.

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
GET  /api/position    → {positions: {ticker: summary | null}}
```

### `routes/trades.py`  blueprint=`trades`
```
GET /trades         → trades.html  (фильтры: direction, exit_reason, date_from, date_to)
GET /trades/export  → CSV-файл
```

### `routes/signals.py`  blueprints=`signals`, `stats`
```
GET /signals         → signals.html  (пагинация)
GET /stats           → stats.html    (aggregates + 2 bar charts)
GET /stats?ticker=X  → то же, но фильтр по тикеру (передаётся instrument_id в repository)
```

### `routes/instruments.py`  blueprint=`instruments`
```
GET  /instruments              → instruments.html  (таблица тикеров + per-ticker статистика)
GET  /instruments/<ticker>/edit → instrument_edit.html  (форма параметров)
POST /instruments/<ticker>/edit → сохранить в instruments.yaml + upsert в БД → redirect
POST /instruments/add          → добавить тикер в yaml (дефолтные параметры) → redirect /edit
```
Чтение/запись `instruments.yaml` через `yaml.safe_load` / `yaml.dump`. Комментарии не сохраняются.
При сохранении: yaml обновляется сразу, стратегия применит изменения только после перезапуска.

### `templates/`
```
base.html             — навигация (Дашборд / Сделки / Сигналы / Статистика / Инструменты), стили
login.html            — standalone (без base)
dashboard.html        — KPI карточки, equity chart, 2 таблицы
trades.html           — таблица с фильтрами + пагинация + CSV кнопка
signals.html          — таблица с пагинацией
stats.html            — тикер-фильтр + 4+4 KPI карточки + 2 bar charts
instruments.html      — таблица тикеров (конфиг + per-ticker P&L/сделки/win%) + форма добавления
instrument_edit.html  — форма редактирования всех параметров тикера (5 секций)
error.html            — 404/500
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
account_id = get_first_account_id()     # SandboxClient или Client
for ticker, params in instruments.items():   # все тикеры из yaml
    strategy, order_manager, position_manager = build_components(ticker, params, account_id)
    recorder = DataRecorder(figi=params["figi"])
    on_orderbook, on_trade = make_event_handlers(strategy, position_manager, recorder)
    stream = StreamHandler(figi, on_orderbook, on_trade, instrument_id=params["instrument_id"])
    threading.Thread(target=stream.start, daemon=True, name=f"stream_{ticker}").start()
    scheduler.add_job(position_manager.check_timeout, "interval", minutes=1, id=f"timeout_check_{ticker}")
scheduler.start()
flask_app = create_app(); set_position_managers({ticker: position_manager, ...})
web_thread.start()
signal.signal(SIGINT/SIGTERM, shutdown)   # graceful stop: stop all streams + set stop_event
stop_event.wait()      # main thread blocks here
```

---

## Соглашения и важные детали

| Что | Где | Детали |
|---|---|---|
| Конфиг → код | всегда через `instrument_config` dict | никаких глобалов в стратегиях |
| Добавить тикер | только `instruments.yaml` | код не трогать |
| Часовой пояс | UTC везде в БД | MSK = UTC+3, конвертация в `combo_strategy._is_trading_hours` и `risk_manager._check_trading_hours` |
| Порог выхода | `ofi_exit_threshold` из конфига | дефолт = `ofi_threshold * 0.5` (обратная совместимость) |
| Принт "свежесть" | ≤ **15 секунд** | в `combo_strategy._check_entry_condition` |
| Идемпотентность ордеров | `uuid4()` как `client_order_id` | `orders` запись создаётся ДО вызова API |
| Только одна позиция | контролируется `RiskManager._check_no_pyramiding` | |
| bot_active флаг | `bot_state.bot_active` в БД | переключается через `POST /api/bot/toggle` |
| Дневной лимит убытков | `DAILY_LOSS_LIMIT_RUB` из `.env` | `repository.get_today_pnl()` |
| Безубыток | `breakeven_ticks` в конфиге | флаг `stop_at_breakeven` в `OpenPosition` |
| Тейк-профит | `take_profit_ticks` в конфиге | 0 = отключён |
| Маркет-дата токен | `TINKOFF_MARKET_TOKEN` | fallback на `TINKOFF_TOKEN` если не задан |
| Запись данных для бэктеста | `RECORD_MARKET_DATA=true` в `.env` | `DataRecorder` → таблицы `market_orderbooks`, `market_trade_ticks` |
| Мультитикер | реализован | все тикеры из `instruments.yaml`, каждый в своём потоке |
