# ARCHITECTURE.md

Справочник структуры проекта для быстрой навигации при правках.
Читай этот файл вместо сканирования всех исходников.

---

## Корневая структура

```
invest-bot/
├── trading_bot/          # Основной пакет
│   ├── backtest/         # Бэктест RSI-стратегии на исторических свечах
│   │   ├── candle_loader.py   # Загрузка 5-мин свечей из T-Invest API + дисковый кэш
│   │   └── engine.py          # Ядро бэктеста: ARSI + OHLC-симулятор → результат
│   ├── config/           # Настройки и конфиг инструментов
│   ├── core/             # Бизнес-логика (стратегия, исполнение, данные, риск)
│   ├── db/               # MySQL-модели, репозиторий, ClickHouse-клиент
│   │   ├── models.py
│   │   ├── repository.py
│   │   └── clickhouse.py
│   ├── notifications/    # Telegram-уведомления
│   │   └── telegram_notifier.py
│   ├── web/              # Flask дашборд
│   └── main.py           # Точка входа
├── migrate_to_clickhouse.py      # Разовый скрипт миграции MySQL → ClickHouse
├── fetch_instruments.py          # Утилита: получить figi/instrument_id из T-Invest API
├── calibrate_multipliers.py      # Ежедневная авто-калибровка print_multiplier (cron 01:00)
├── backtest_rsi.py               # CLI бэктест: --ticker SBER --days 60 | --all --days 90
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
        │     └─▶ repository.save_orderbook_snapshot / save_trade_tick
        │           └─▶ ClickHouseWriter (если CLICKHOUSE_HOST задан)
        │               или MarketOrderbook/MarketTradeTick в MySQL (fallback)
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
                          ├─▶ OrderManager.place_market_order()
                          │     └─▶ T-Invest orders.post_order() / sandbox.post_sandbox_order()
                          │     └─▶ repository.save_order / update_order_status / save_trade
                          └─▶ TelegramNotifier.send_position_opened/closed()

PositionManager.update_market_price(price)   # вызывается при каждой сделке из стрима
  └─▶ _check_stop_loss(price)
        ├─▶ трейлинг-стоп: peak_price обновляется; стоп = max(initial, peak - trail_distance)
        ├─▶ безубыток: если gain >= breakeven_ticks → stop_at_breakeven = True
        ├─▶ стоп-лосс: loss >= stop_ticks (или цена вернулась за вход при безубытке)
        └─▶ тейк-профит: gain >= take_profit_ticks (если take_profit_ticks > 0)
```

Фоновые потоки в `main.py`:
- `stream_thread` (daemon) — стрим рыночных данных, по одному на тикер
- `web_thread` (daemon) — Flask дашборд
- `ch_flush` (daemon) — сброс буферов ClickHouse каждые 5 секунд
- `APScheduler` — `position_manager.check_timeout()` + `portfolio_manager.refresh()` каждую минуту; cron 10:05 МСК (пн-пт) → Telegram-уведомление о начале торгов

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
DAILY_LOSS_LIMIT_PCT: float            # default 0.01 — лимит убытка в % от счёта (1%)
DAILY_LOSS_LIMIT_RUB: float            # default -500.0 — fallback если портфель не загружен
MAX_GLOBAL_POSITIONS: int              # default 3 — макс. одновременных позиций по всем тикерам
MAX_POSITION_PCT: float                # default 0.30 — доля портфеля на одну сделку (30%)
USE_SANDBOX: bool                      # SANDBOX=true в .env
LOG_DIR / LOG_FILE / LOG_LEVEL
INSTRUMENTS_CONFIG_PATH                # → trading_bot/config/instruments.yaml
TELEGRAM_BOT_TOKEN: str                # токен Telegram-бота (опционально; без токена — no-op)
TELEGRAM_CHAT_ID: str                  # ID чата для уведомлений
CLICKHOUSE_HOST: str                   # "" = ClickHouse отключён, писать в MySQL
CLICKHOUSE_PORT: int                   # default 8123
CLICKHOUSE_USER: str                   # default "default"
CLICKHOUSE_PASSWORD: str               # default ""
CLICKHOUSE_DATABASE: str               # default "trading_bot"
RECORD_MARKET_DATA: bool               # писать стаканы/тики для бэктеста
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
  print_multiplier: 50.0      # объём >= медиана * multiplier → крупный принт; авто-калибруется ежедневно (calibrate_multipliers.py): target ~p97 = (p95/median + p99/median) / 2, clamp [10, 200], порог обновления 20%
  print_window: 200           # размер окна медианы объёмов
  print_max_age_seconds: 15   # принт старше этого — не считается (15с для ликвидных, 25-30с для неликвидных)

  # Cooldown
  cooldown_seconds: 60        # между сигналами входа
  post_close_cooldown_seconds: 90  # после закрытия позиции (защита от флипа)

  # Управление позицией
  max_hold_minutes: 60
  ofi_scale: 1000                    # масштаб tanh-нормализации (подбирается под ликвидность инструмента)
  ofi_auto_calibrate_window: 0       # > 0: первые N снапшотов → p90(|raw_ofi|) заменяет ofi_scale; 0 = откл
  trend_ma_window: 1000              # окно MA mid-цен для фильтра тренда (0 = выкл)
  min_ofi_entry_confirmations: 3     # N подряд OFI-чтений выше порога до генерации сигнала входа
  max_position_lots: 500             # жёсткий потолок лотов; не должен мешать расчёту 30% — compute_lots считает долю портфеля динамически

  # Стопы и тейк
  tick_size: 0.01
  stop_ticks: 40
  breakeven_ticks: 35         # перенести стоп в безубыток после N тиков в плюс (только если trailing_stop_ticks=0)
  take_profit_ticks: 120      # 0 = отключён
  trailing_stop_ticks: 30     # трейлинг-стоп: отстаёт от пика на N тиков; 0 = откл

  trading_hours: {start: "10:05", end: "18:30"}
  skip_first_minutes: 5
```
Добавить тикер = добавить секцию сюда + перезапустить (в БД попадёт автоматически).
Новые тикеры (instrument_id неизвестен) → запустить `python fetch_instruments.py`.

Тикеры в yaml (20 штук): SBER, GMKN, VTBR, LKOH, GAZP, NVTK, ROSN, TATN, YNDX, PLZL,
CHMF, NLMK, MAGN, ALRS, MTSS, SIBN, AFLT, MGNT, MOEX, PHOR.

---

## core/strategy/

### `base_strategy.py`
```python
@dataclass Signal:
    signal_type: SignalType   # LONG | SHORT | EXIT
    reason: SignalReason      # COMBO_TRIGGERED | OFI_REVERSED | TIMEOUT | STOP_LOSS | TAKE_PROFIT | TRAILING_STOP | MANUAL
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
    __init__(ofi_levels: int, smooth_window: int = 1, ofi_scale: float = 1000.0, calibrate_window: int = 0)
    update(bids, asks) → float | None   # None на первом снапшоте
    reset()
    # properties: last_ofi, is_calibrated
```
Алгоритм: Cont-Kukanov-Stoikov по топ N уровней. Нормализация через `tanh(raw / ofi_scale)`.
`ofi_scale` берётся из конфига инструмента (дефолт 1000). Подбирается под ликвидность: при raw OFI ≈ ofi_scale → tanh ≈ 0.76. Слишком большой → OFI всегда около 0; слишком маленький → всегда ±1.
Сглаживание: скользящее среднее по `smooth_window` последних значений.
`_prev_bids/_prev_asks` хранят предыдущий снапшот для вычисления дельты.

**Авто-калибровка** (`calibrate_window > 0`): первые N снапшотов накапливают `|raw_ofi|`, затем p90 заменяет `ofi_scale`. Логика: при `raw_ofi = p90 → tanh(1) ≈ 0.76`, то есть 90% обычных снапшотов дадут OFI ниже порога входа. После калибровки в лог пишется `scale X → Y`. Для уже откалиброванных инструментов держать `ofi_auto_calibrate_window: 0`.

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
**Вход:** `|OFI| >= ofi_threshold` на `min_ofi_entry_confirmations` подряд + принт той же стороны + свежесть принта ≤ **15с** + cooldown + фильтр тренда.
**Фильтр тренда:** если `trend_ma_window > 0` — LONG разрешён только при `mid > MA(N)`, SHORT — при `mid < MA(N)`. До заполнения окна входы блокируются. `0` = отключён.
**Подтверждения входа:** `_ofi_entry_confirmations` — счётчик последовательных OFI-чтений в одном направлении. При смене направления сбрасывается. Сигнал только при `count >= min_ofi_entry_confirmations`.
**Выход:** только через stop-loss / take-profit / trailing-stop / timeout (PositionManager). OFI-разворот как причина выхода — отключён.

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

### `portfolio_manager.py`
```python
class PortfolioManager:
    __init__(account_id, max_positions, max_position_pct, figis: list[str])
    refresh()                              # обновить портфель + последние цены (вызывает оба метода ниже)
    get_price(figi) → float | None         # последняя цена из API (None если не загружена)
    register_opened(instrument_id: int)    # зарегистрировать открытую позицию
    register_closed(instrument_id: int)    # снять регистрацию закрытой позиции
    can_open() → bool                      # open_positions_count < max_positions
    compute_lots(figi, lot_size, stream_price, max_lots_cap) → int
    get_summary() → dict                   # для /api/account
    # properties: portfolio_value, open_positions_count
```
Один экземпляр на весь бот — разделяется между всеми `PositionManager`.
`refresh()` вызывается при старте + каждую минуту через APScheduler (`portfolio_refresh`).
Внутри `refresh()` два вызова API: `get_sandbox_portfolio` / `get_portfolio` (баланс) + `get_last_prices(figi=[...])` (цены всех инструментов).
`compute_lots` — приоритет цены: 1) `stream_price` из стрима (свежее); 2) `_last_prices[figi]` из API refresh; 3) fallback `max_lots_cap` или 1 с WARNING.
Формула: `floor(portfolio * max_position_pct / (price * lot_size))`, min=1, cap=`max_position_lots` из конфига.

### `position_manager.py`
```python
@dataclass OpenPosition:
    direction, entry_price, quantity_lots, open_at, open_order_id, signal_id, current_price
    stop_at_breakeven: bool    # флаг: безубыток активирован (только при trailing_stop_ticks=0)
    peak_price: float          # лучшая цена с момента входа (для трейлинг-стопа)
    # computed: unrealized_pnl, hold_seconds

class PositionManager:
    __init__(instrument_id, instrument_config, order_manager, strategy,
             portfolio_manager=None, ticker="", notifier=None)
    on_signal(signal: Signal)           # главная точка входа
    update_market_price(price: float)   # вызывать при каждой сделке → проверяет стоп/тейк
    check_timeout()                     # вызывать из планировщика каждую минуту
    get_position_summary() → dict | None
    # properties: open_position, has_position
    # internal: _last_price (последняя рыночная цена, для compute_lots при открытии)
```
**P&L:** `(close - open) * lots * lot_size - commission`. Комиссия суммируется из обоих ордеров.
**Стоп-лосс:** `stop_ticks * tick_size` (tick_size из конфига, дефолт 0.01).
**Трейлинг-стоп:** если `trailing_stop_ticks > 0` — стоп следует за ценовым пиком на расстоянии `trailing_stop_ticks * tick_size`. Начальная защита = `stop_ticks` до тех пор, пока трейлинг не обгонит её. `peak_price` обновляется в `OpenPosition` при каждом `update_market_price`. Закрытие с `exit_reason="trailing_stop"`.
**Безубыток:** используется только если `trailing_stop_ticks=0`. После движения на `breakeven_ticks` в плюс стоп переносится на цену входа (`stop_at_breakeven = True`). При возврате за вход — `exit_reason="breakeven_stop"`.
**Тейк-профит:** если `take_profit_ticks > 0` и движение в плюс достигло порога — закрытие с `exit_reason="take_profit"`.
**Глобальный лимит позиций:** в `on_signal` перед открытием проверяется `portfolio_manager.can_open()`. При достижении лимита сигнал блокируется с WARNING.
**Размер лота:** при открытии вычисляется через `portfolio_manager.compute_lots(_last_price, lot_size, max_position_lots)`.
**Уведомления:** `TelegramNotifier.send_position_opened()` после открытия. `send_position_closed()` вызывается **до** `repository.save_trade()` — чтобы ошибка БД не заблокировала получение результата сделки в Telegram.

---

## core/risk/

### `risk_manager.py`
```python
class RiskCheckFailed(Exception): ...

class RiskManager:
    __init__(instrument_id, instrument_config, portfolio_manager=None)
    check_all(signal_type, has_open_position, current_position_direction)
    # приватные: _check_bot_active, _check_trading_hours, _check_no_pyramiding, _check_daily_loss_limit
    _deny(reason, message)   # логирует в БД + бросает RiskCheckFailed
```
Порядок проверок: bot_active → trading_hours → пирамидинг → дневной лимит убытков.
**Дневной лимит:** `portfolio_value * DAILY_LOSS_LIMIT_PCT` (1% от счёта по умолчанию).
Fallback на `DAILY_LOSS_LIMIT_RUB` если `portfolio_manager` не передан или портфель не загружен.

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
Вызывает `repository.save_orderbook_snapshot` / `save_trade_tick`, которые сами решают куда писать (ClickHouse или MySQL).
При `RECORD_MARKET_DATA=false` — no-op. Запись только в `trading_hours` (MSK).

---

## db/

### `models.py` — таблицы SQLAlchemy (MySQL)

| Таблица | Ключевые поля |
|---|---|
| `instruments` | ticker, figi, is_active, все параметры стратегии |
| `signals` | instrument_id, signal_type(long/short/exit), ofi_value, print_volume, print_side, reason, acted_on |
| `orders` | instrument_id, signal_id, order_id_broker, direction, quantity, price_executed, status, commission_rub |
| `trades` | open/close_order_id, direction, open/close_price, pnl_rub, commission_rub, open/close_at, hold_seconds, exit_reason |
| `bot_logs` | level(INFO/WARNING/ERROR), component, message |
| `users` | username, password_hash, is_active, last_login |
| `bot_state` | id=1 (singleton), bot_active |
| `market_orderbooks` | figi, bids(JSON), asks(JSON), recorded_at — **только если CLICKHOUSE_HOST не задан** |
| `market_trade_ticks` | figi, price, quantity, direction, recorded_at — **только если CLICKHOUSE_HOST не задан** |

`exit_reason` значения в `trades`: `ofi_reversed`, `timeout`, `stop_loss`, `breakeven_stop`, `take_profit`, `trailing_stop`, `manual`.

### `clickhouse.py` — хранение маркет-данных

```python
class ClickHouseWriter:
    # Singleton. Буферизует строки и пишет батчами каждые 5 сек или при 1000 строках.
    insert_orderbook(figi, bids, asks, timestamp)
    insert_trade_tick(figi, price, quantity, direction, timestamp)
    flush()   # принудительный сброс буферов (вызывается atexit)
    query_orderbooks(figi, date_from, date_to) → list of (ts, bids, asks)
    iter_orderbooks(figi, date_from, date_to, chunk_size) → Generator
    query_trade_ticks(figi, date_from, date_to) → list of (ts, price, qty, dir)
    iter_trade_ticks(figi, date_from, date_to, chunk_size) → Generator
    query_recorded_dates(figi) → List[str]
    count_orderbooks(figi=None) → int
    count_trade_ticks(figi=None) → int

is_enabled() → bool          # True если CLICKHOUSE_HOST задан
get_writer() → ClickHouseWriter   # singleton
init_clickhouse()             # вызывается из main.py при старте
```

ClickHouse таблицы: `market_orderbooks`, `market_trade_ticks`.
Движок: `MergeTree`, партиционирование по месяцу (`toYYYYMM(recorded_at)`), сортировка по `(figi, recorded_at)`.

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
delete_trade(trade_id) → bool
get_today_pnl(instrument_id=None) → float
get_trades_page(page, per_page, direction, exit_reason, date_from, date_to) → (List, total)
get_all_trades_for_export(...) → List[Trade]

# Aggregates (для веба)
get_stats_summary(instrument_id=None) → dict   # total_trades, win_rate, total_pnl, avg_win, avg_loss, profit_factor, best/worst, avg_hold
get_pnl_by_day(days=30) → List[{day, pnl}]
get_pnl_by_hour(instrument_id=None) → List[{hour, pnl, count}]
get_pnl_by_weekday(instrument_id=None) → List[{dow, pnl, count}]
# instrument_id=None → все тикеры; передать int → фильтр по инструменту

# Market data (бэктест) — автоматически выбирают ClickHouse или MySQL
save_orderbook_snapshot(figi, bids, asks, timestamp)
save_trade_tick(figi, price, quantity, direction, timestamp)
get_orderbook_snapshots(figi, date_from, date_to) → list
iter_orderbook_snapshots(figi, date_from, date_to, chunk_size) → Generator[(ts, bids, asks)]
get_trade_ticks(figi, date_from, date_to) → list
iter_trade_ticks(figi, date_from, date_to, chunk_size) → Generator[(ts, price, qty, dir)]
get_recorded_dates(figi) → List[str]

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

## notifications/

### `telegram_notifier.py`
```python
class TelegramNotifier:
    __init__(token: str, chat_id: str)
    send_bot_started(tickers, sandbox)          # при запуске бота
    send_trading_day_started(tickers)           # 10:05 МСК пн-пт (APScheduler cron)
    send_position_opened(ticker, direction, entry_price, quantity_lots, lot_size)
    send_position_closed(ticker, direction, entry_price, close_price,
                         quantity_lots, lot_size, pnl, hold_seconds, exit_reason)
```
Все `send_*` неблокирующие — кладут текст в `queue.Queue`, которую читает один фоновый daemon-поток (`_worker`).
До 3 попыток на каждое сообщение: при 429 (rate limit) ждёт `retry_after` из ответа API; при сетевой ошибке — экспоненциальная задержка (1с, 2с).
Если `TELEGRAM_BOT_TOKEN` или `TELEGRAM_CHAT_ID` не заданы — worker не запускается, все методы no-op.

---

## web/

### `app.py`
```python
create_app() → Flask   # регистрирует blueprints + Flask-Login
get_position_managers() → dict[ticker, PositionManager]
set_position_managers(pms: dict) → None
get_portfolio_manager() → PortfolioManager | None
set_portfolio_manager(pm) → None
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
GET  /api/account     → {portfolio_value, open_positions, max_positions, max_position_pct}
```

### `routes/trades.py`  blueprint=`trades`
```
GET  /trades                  → trades.html  (фильтры: direction, exit_reason, date_from, date_to)
GET  /trades/export           → CSV-файл
POST /trades/<id>/delete      → удалить сделку, редирект на /trades
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

### `routes/strategies.py`  blueprint=`strategies`
```
GET  /strategies                      → strategies.html  (карточки стратегий: статус, P&L, toggle)
POST /api/strategies/<name>/toggle    → {active: bool}   (включить/выключить стратегию)
```
Выключение стратегии блокирует открытие новых позиций (entry-сигналы игнорируются в
`PositionManager.on_signal`). Уже открытые позиции продолжают работать.
Состояние хранится в таблице `strategy_state` (перманентно, переживает перезапуск).

### `routes/backtest.py`  blueprint=`backtest`
```
GET  /backtest                        → backtest.html  (панель управления + сводная таблица + детали)
POST /api/backtest/run                → {job_id, total}  (запускает фоновый поток)
GET  /api/backtest/status/<job_id>    → {status, progress, total, current_ticker}  (polling)
GET  /api/backtest/results            → [{ticker, n_trades, win_rate, total_pnl, ...}]  (сводка)
GET  /api/backtest/results/<ticker>   → {trades, equity_curve, metrics, ...}  (детали)
```

### `routes/optimize.py`  blueprint=`optimize`
```
GET  /optimize                        → optimize.html  (grid search + сводная таблица + детали)
POST /api/optimize/run                → {job_id, total, combos_per_ticker}
GET  /api/optimize/status/<job_id>    → {status, progress, total, combo_progress, combo_total, current_ticker}
GET  /api/optimize/results            → [{ticker, current_pf, best_pf, best_trades, ...}]  (сводка)
GET  /api/optimize/results/<ticker>   → {current_params, current_metrics, top_configs, ...}  (детали)
POST /api/optimize/apply/<ticker>     → {ok, applied_params}  (записать в rsi_config.yaml)
```
Применяет только: ob_value, os_value, stop_ticks, take_profit_ticks, trailing_stop_ticks,
breakeven_ticks, atr_ratio_min. Остальные параметры не трогает.
Запуск бэктеста — фоновый daemon-поток (`threading.Thread`). Прогресс отслеживается polling'ом каждые 1.5с.
Результаты кэшируются в памяти (`_results: dict[ticker, dict]`) до следующего запуска или рестарта Flask.
При повторном запуске кэш перезаписывается.

### `templates/`
```
base.html             — навигация (Дашборд / Сделки / Сигналы / Статистика / Инструменты / Стратегии / RSI-тест / Бэктест)
login.html            — standalone (без base)
dashboard.html        — KPI карточки, equity chart, 2 таблицы
trades.html           — таблица с фильтрами (включая фильтр по стратегии) + пагинация + CSV
signals.html          — таблица с пагинацией
stats.html            — тикер-фильтр + стратегия-фильтр + breakdown-карточки по стратегиям + 4+4 KPI + 2 bar charts
instruments.html      — таблица тикеров (конфиг + per-ticker P&L/сделки/win%) + форма добавления
instrument_edit.html  — форма редактирования всех параметров тикера (5 секций)
strategies.html       — карточки стратегий: описание, статус, P&L, кнопка включить/выключить
backtest.html         — панель запуска, прогресс-бар, сводная таблица по тикерам, детали (equity curve + сделки)
error.html            — 404/500
```
Chart.js подключается через CDN (`cdn.jsdelivr.net/npm/chart.js@4.4.0`).

---

## main.py — последовательность старта

```python
setup_logging()              # RotatingFileHandler(logs/bot.log) + stdout
repository.init_db()         # CREATE TABLE IF NOT EXISTS + BotState + StrategyState singeltons
init_clickhouse()            # подключить ClickHouse если CLICKHOUSE_HOST задан (no-op иначе)
ensure_default_user(...)     # создать admin если нет
config = load_instruments_config()      # читает instruments.yaml
instruments = sync_instruments_to_db()  # upsert в таблицу instruments
rsi_config = load_rsi_config()          # читает rsi_config.yaml
account_id = get_first_account_id()     # SandboxClient или Client
notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
portfolio_manager = PortfolioManager(account_id, MAX_GLOBAL_POSITIONS, MAX_POSITION_PCT)
portfolio_manager.refresh()             # загрузить баланс сразу при старте

# ── Combo-стратегия (один стрим на тикер) ────────────────────────────────────
for ticker, params in instruments.items():
    strategy, order_manager, position_manager = build_components(
        ticker, params, account_id, portfolio_manager, notifier)   # strategy_name='combo'
    recorder = DataRecorder(figi=params["figi"])
    on_orderbook, on_trade = make_event_handlers(strategy, position_manager, recorder)
    stream = StreamHandler(figi, on_orderbook, on_trade, instrument_id=params["instrument_id"])
    threading.Thread(target=stream.start, daemon=True, name=f"stream_{ticker}").start()
    scheduler.add_job(position_manager.check_timeout, "interval", minutes=1, id=f"timeout_check_{ticker}")

# ── RSI-стратегия (отдельный стрим на тикер из rsi_config.yaml) ──────────────
for ticker, rsi_params in rsi_config.items():
    strategy, order_manager, position_manager = build_rsi_components(
        ticker, instruments[ticker], rsi_params, account_id, portfolio_manager, notifier)  # strategy_name='rsi'
    _, on_trade_rsi = make_event_handlers(strategy, position_manager, recorder_rsi)
    rsi_stream = StreamHandler(figi, on_orderbook_rsi, on_trade_rsi, instrument_id=...)
    threading.Thread(target=rsi_stream.start, daemon=True, name=f"rsi_stream_{ticker}").start()
    scheduler.add_job(position_manager.check_timeout, "interval", minutes=1, id=f"rsi_timeout_{ticker}")

scheduler.add_job(portfolio_manager.refresh, "interval", minutes=1, id="portfolio_refresh")
scheduler.add_job(notifier.send_trading_day_started, "cron",
                  day_of_week="mon-fri", hour=7, minute=5, id="trading_day_start_notify")
scheduler.start()
notifier.send_bot_started(tickers, sandbox)
flask_app = create_app(); set_position_managers(...); set_portfolio_manager(portfolio_manager)
web_thread.start()
signal.signal(SIGINT/SIGTERM, shutdown)   # graceful stop: stop streams + flush CH
stop_event.wait()
```

---

## RSI-стратегия (новая, 5-минутные свечи)

### Новые файлы
```
trading_bot/core/strategy/candle_aggregator.py  — агрегатор 5-мин свечей из тиков
trading_bot/core/strategy/rsi_strategy.py       — Augmented RSI стратегия
trading_bot/config/rsi_config.yaml              — параметры RSI на тикер
migrate_add_strategy_name.py                    — разовая миграция БД
STRATEGY.md                                     — описание стратегий для пользователя
```

### `candle_aggregator.py`
```python
class Candle:
    open_time, open, high, low, close, volume, trade_count
    update(price, volume)

class CandleAggregator:
    __init__(interval_minutes: int, on_candle_closed: Callable[[Candle], None])
    on_trade(price, volume, timestamp)   # при смене периода → вызывает on_candle_closed
    reset()
```
Свечи формируются по UTC-времени. Ключ периода = `(час*60 + минута) // interval * interval`.

### `rsi_strategy.py`
```python
class AugmentedRSI:
    __init__(length, smooth, smo_type_rsi='RMA', smo_type_signal='EMA')
    update(close) → (arsi, signal) | None

class RSIStrategy(BaseStrategy):
    on_orderbook(data)          → no-op
    on_trade(data)              → кормит CandleAggregator
    get_signal()                → Signal | None
    set_position(direction, close_time)
    warmup(closes: list[float]) → прогрев RMA на исторических данных
    update_atr(short, long)     → обновить ATR-фильтр (из планировщика)
    current_ofi                 → last_arsi (для совместимости с PositionManager)
```
**Алгоритм Augmented RSI (LuxAlgo):**
`diff = r если upper растёт; -r если lower падает; d=close−prev иначе`
`arsi = rma(diff)/rma(|diff|) * 50 + 50`  диапазон [0, 100]
`signal = ema(arsi, smooth)`

**Входы:** arsi пересекает os_value снизу вверх → LONG; ob_value сверху вниз → SHORT.
**Выход:** только через stop-loss / take-profit / trailing-stop / timeout (PositionManager). Выход по signal line — отключён.

**Прогрев при старте:** `warmup_rsi_strategy()` в `main.py` загружает последние `warmup_candles` (default 500) 5-минутных свечей из API и прогоняет через `AugmentedRSI.update()`. Без прогрева RMA не сходится и значения расходятся с TradingView.
⚠️ **T-Invest API ограничение:** для `CANDLE_INTERVAL_5_MIN` максимум 1 день за запрос. `warmup_rsi_strategy` пагинирует по 1 дню за 14 календарных дней (аналогично `/rsi-test`). Без пагинации бот получает только ~78 свечей → RMA не сходится → неправильные ARSI-значения → ложные сигналы.

**ATR-фильтр активности:** каждые 5 минут `refresh_rsi_atr()` загружает свечи за `atr_days` дней, считает `short_atr` (последние N свечей) и `long_atr` (среднее за все дни). Если `short_atr / long_atr < atr_ratio_min` — рынок неактивен, вход заблокирован.

### `rsi_config.yaml`
Структура: один тикер = одна секция. Тикеры должны совпадать с `instruments.yaml`.
Параметры: `length`, `smooth`, `smo_type_rsi`, `smo_type_signal`, `ob_value`, `os_value`,
`stop_ticks`, `breakeven_ticks`, `take_profit_ticks`, `trailing_stop_ticks`,
`max_position_lots`, `max_hold_minutes`, `cooldown_seconds`, `post_close_cooldown_seconds`,
`trading_hours`, `skip_first_minutes`, `min_hold_seconds`,
`atr_days` (default 5), `atr_length_short` (default 5), `atr_ratio_min` (default 0 = выкл),
`warmup_candles` (default 500).
figi / instrument_id / lot_size / tick_size / commission_rate берутся из `instruments.yaml`.

### Новые поля БД
```
signals.strategy_name  VARCHAR(50) DEFAULT 'combo'  — 'combo' или 'rsi'
trades.strategy_name   VARCHAR(50) DEFAULT 'combo'  — 'combo' или 'rsi'
```
Новая таблица:
```
strategy_state: strategy_name (PK), is_active (bool), updated_at
```
Записи инициализируются в `init_db()` → `init_strategy_states()`.

### strategy_name в PositionManager
`PositionManager.__init__` принимает `strategy_name: str = 'combo'`.
`on_signal` проверяет `repository.get_strategy_active(strategy_name)` перед открытием.
Все `save_signal` и `save_trade` вызываются через `_save_signal` / `_save_trade` хелперы,
которые автоматически прокидывают `strategy_name`.

---

## backtest/ — бэктест RSI на исторических свечах

### `candle_loader.py`
```python
load_candles(figi: str, ticker: str, days: int) -> List[OHLCVCandle]
# OHLCVCandle = TypedDict: time, open, high, low, close, volume
```
Загружает 5-мин свечи из T-Invest API с пагинацией по 1 дню (то же ограничение API, что и в warmup).
**Дисковый кэш:** завершённые дни сохраняются в `~/.cache/invest-bot/candles/{ticker}_{date}.pkl`.
При повторном запуске кэшированные дни не запрашиваются. Текущий (неполный) день — всегда свежий.
`--no-cache` в CLI удаляет pkl-файлы тикера перед загрузкой.

### `engine.py`
```python
run_backtest(
    candles: List[OHLCVCandle],
    rsi_params: dict,          # секция из rsi_config.yaml
    instrument_params: dict,   # {ticker, lot_size, tick_size, commission_rate}
    warmup_candles: int = 300,
    days: int = 0,
) -> dict   # {ticker, days, candles_total, candles_used, trades, equity_curve, metrics, run_at}
```
**Логика симулятора:**
- Прогрев: первые `warmup_candles` свечей кормятся в `AugmentedRSI`, позиции не открываются
- Сигнал (crossover ARSI через ob/os) → `pending_signal`; вход по `open` следующей свечи
- На каждой свече открытой позиции: обновить трейлинг/безубыток → проверить стоп/тейк по `high`/`low`
- **Пессимизм:** если стоп и тейк задеты в одной свече — стоп побеждает
- Тайм-аут: `hold_candles >= max_hold_candles` (max_hold_minutes / 5)
- ATR-фильтр: rolling `short_atr / long_atr`, вычисляется inline по true range каждой свечи
- Кулдауны, торговые часы — идентичны live RSIStrategy
- PnL считается на **1 лот** (quantity_lots=1) для нормализованного сравнения между тикерами

**Метрики в `metrics` dict:** `n_trades`, `win_rate`, `total_pnl`, `profit_factor`, `max_drawdown`, `avg_hold_candles`, `exit_reasons: {reason: count}`.

### `optimizer.py`
```python
optimize_ticker(
    candles, rsi_params_base, instrument_params,
    warmup_candles=300, grid=None, progress_cb=None, min_trades=10
) -> List[Dict]   # top-10 конфигов {params, metrics} по profit_factor убыванию

total_combos(grid=None) -> int   # число комбинаций в сетке (729 по умолчанию)

DEFAULT_GRID: {
    ob_value: [75, 80, 85], os_value: [15, 20, 25],
    stop_mult: [0.7, 1.0, 1.4],   # × текущий stop_ticks
    take_ratio: [2, 3, 4],         # × stop_ticks
    trail_ratio: [0, 0.5, 0.7],    # × stop_ticks (0=выкл)
    atr_ratio_min: [0, 0.5, 0.7],
}
```
Grid search по 729 комбинациям (os < ob автоматически фильтруется).
Изменяет только: ob/os, stop/take/trailing/breakeven ticks, atr_ratio_min.
Остальные параметры берутся из базового конфига без изменений.
Результат НЕ применяется автоматически — только через `POST /api/optimize/apply/<ticker>`.

### `backtest_rsi.py` (CLI)
```bash
python backtest_rsi.py --ticker SBER --days 60
python backtest_rsi.py --all --days 90
python backtest_rsi.py --all --days 60 --no-cache
```
Выводит таблицу метрик в stdout. Сортировка по profit_factor (убывание).

---

## Соглашения и важные детали

| Что | Где | Детали |
|---|---|---|
| Конфиг → код | всегда через `instrument_config` dict | никаких глобалов в стратегиях |
| Добавить тикер | только `instruments.yaml` | код не трогать; новые instrument_id брать из `fetch_instruments.py` |
| Часовой пояс | UTC везде в БД | MSK = UTC+3, конвертация в `combo_strategy` и `risk_manager` |
| Порог выхода | `ofi_exit_threshold` из конфига | дефолт = `ofi_threshold * 0.5` (обратная совместимость) |
| Принт "свежесть" | ≤ **15 секунд** | в `combo_strategy._check_entry_condition` |
| Идемпотентность ордеров | `uuid4()` как `client_order_id` | `orders` запись создаётся ДО вызова API |
| Только одна позиция | контролируется `RiskManager._check_no_pyramiding` | |
| bot_active флаг | `bot_state.bot_active` в БД | переключается через `POST /api/bot/toggle` |
| strategy_name тег | `signals.strategy_name`, `trades.strategy_name` | `'combo'` или `'rsi'`; NULL у старых записей → трактуется как `'combo'` |
| Включение стратегий | `strategy_state.is_active` в БД | `POST /api/strategies/<name>/toggle`; блокирует только новые входы |
| RSI warmup — пагинация | `warmup_rsi_strategy()` в `main.py` | API лимит: 1 день за запрос для 5-мин свечей; запрашивает 14 дней по 1 дню; без этого ARSI расходится с TradingView и бот открывает ложные сделки |
| RSI и Combo — разные потоки | по одному `stream_thread` на каждую стратегию/тикер | потоки изолированы, один `PositionManager` на поток |
| RSI — торговые часы | `trading_hours: start: '10:05', end: '22:00'` | новые входы разрешены 10:05-22:00 МСК (дневная + вечерняя сессия MOEX); вне окна `RiskManager` отказывает |
| RSI — принудительное EOD-закрытие | `eod_close_time: '23:30'` в `rsi_config.yaml` | `check_eod_close()` вызывается планировщиком каждую минуту; если есть открытая позиция и МСК ≥ 23:30 — принудительно закрывает с `exit_reason='eod_close'`. Обходит RiskManager (напрямую через `_close_position`) |
| RSI — `SignalReason.EOD_CLOSE` | `base_strategy.py` | значение `'eod_close'`; используется только RSI для EOD-закрытия |
| Дневной лимит убытков | `DAILY_LOSS_LIMIT_PCT=0.01` (1% от счёта) | fallback `DAILY_LOSS_LIMIT_RUB=-500` если портфель не загружен |
| Безубыток | `breakeven_ticks` в конфиге | флаг `stop_at_breakeven` в `OpenPosition`; игнорируется если `trailing_stop_ticks > 0` |
| Тейк-профит | `take_profit_ticks` в конфиге | 0 = отключён |
| Трейлинг-стоп | `trailing_stop_ticks` в конфиге | 0 = отключён; `peak_price` в `OpenPosition` |
| Подтверждения входа | `min_ofi_entry_confirmations` в конфиге | N последовательных OFI ≥ threshold |
| Маркет-дата токен | `TINKOFF_MARKET_TOKEN` | fallback на `TINKOFF_TOKEN` если не задан |
| Хранение маркет-данных | ClickHouse если `CLICKHOUSE_HOST` задан | иначе MySQL; буфер 1000 строк / 5 сек |
| Миграция MySQL → CH | `python migrate_to_clickhouse.py` | сначала `--dry-run`; удаляет из MySQL после успешной вставки |
| Калибровка multiplier | `python calibrate_multipliers.py` | cron 01:00 ежедневно; обновляет `print_multiplier` в `instruments.yaml` на основе CH за 10 дней; бот рестартует в 01:05 через `systemctl restart trading-bot` |
| Мультитикер | реализован | все тикеры из `instruments.yaml`, каждый в своём потоке |
| Глобальный лимит позиций | `MAX_GLOBAL_POSITIONS` в `.env` (дефолт 3) | `PortfolioManager.can_open()` → блокирует в `PositionManager.on_signal` |
| Размер позиции от портфеля | `MAX_POSITION_PCT` в `.env` (дефолт 0.30) | `PortfolioManager.compute_lots()`: 30% депо / (цена × lot_size); cap = `max_position_lots` |
| Баланс счёта | `GET /api/account` | `PortfolioManager.refresh()` каждую минуту |
| Telegram уведомления | `TelegramNotifier` | запуск бота, начало торгов (10:05 МСК), открытие/закрытие позиции |
| Бэктест — источник данных | T-Invest API 5-мин свечи | пагинация по 1 дню; кэш в `~/.cache/invest-bot/candles/`; не использует ClickHouse/MySQL |
| Бэктест — PnL | 1 лот, не портфель | для сравнения стратегий между тикерами; комиссия включена |
| Бэктест — пессимизм стоп/тейк | стоп побеждает если оба в одной свече | стандартное допущение OHLC-бэктестов |
| Бэктест — результаты | in-memory `_results` в `routes/backtest.py` | не персистируются; пересчитываются при новом запуске |
