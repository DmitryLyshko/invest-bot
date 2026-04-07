# Trading Bot — MOEX OFI + Large Print Strategy

Алгоритмический торговый бот для Московской биржи на базе стратегии
**OFI (Order Flow Imbalance)** + **крупные принты**. Работает через T-Invest API,
хранит данные в MySQL, имеет веб-дашборд на Flask.

---

## Содержание

1. [Быстрый старт](#быстрый-старт)
2. [Установка](#установка)
3. [Конфигурация](#конфигурация)
4. [Как добавить новый инструмент](#как-добавить-новый-инструмент)
5. [Как подбирать параметры](#как-подбирать-параметры)
6. [Как читать дашборд](#как-читать-дашборд)
7. [Архитектура](#архитектура)

---

## Быстрый старт

```bash
# 1. Клонировать репозиторий
git clone <repo> && cd invest-bot

# 2. Создать виртуальное окружение
python3.11 -m venv .venv && source .venv/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Настроить переменные окружения
cp .env.example .env
# Отредактировать .env: добавить TINKOFF_TOKEN, MYSQL_URL, WEB_PASSWORD

# 5. Создать базу данных MySQL
mysql -u root -e "CREATE DATABASE trading_bot CHARACTER SET utf8mb4;"
mysql -u root -e "CREATE USER 'trading_user'@'localhost' IDENTIFIED BY 'trading_pass';"
mysql -u root -e "GRANT ALL ON trading_bot.* TO 'trading_user'@'localhost';"

# 6. Запустить бот (таблицы создаются автоматически)
python -m trading_bot.main
```

Дашборд будет доступен на `http://127.0.0.1:5000`

---

## Установка

### Требования
- Python 3.11+
- MySQL 8.0+
- T-Invest аккаунт с API токеном

### Получение T-Invest токена
1. Зайти на [tinkoff.ru/invest](https://www.tinkoff.ru/invest/)
2. Настройки → API → Создать токен
3. Выбрать права: **Торговые операции** (для реальной торговли) или **Только чтение** (для тестов)

### Первый запуск в Sandbox
Установить `SANDBOX=true` в `.env` — ордера будут тестовыми, без реальных денег.
Счёт в sandbox нужно активировать через API или Tinkoff Invest приложение.

---

## Конфигурация

### .env файл

| Переменная | Описание | Пример |
|---|---|---|
| `TINKOFF_TOKEN` | API токен T-Invest | `t.xxx...` |
| `MYSQL_URL` | URL подключения к MySQL | `mysql+pymysql://user:pass@localhost/trading_bot` |
| `WEB_SECRET_KEY` | Секрет для Flask сессий | случайная строка ≥32 символа |
| `WEB_USERNAME` | Логин для дашборда | `admin` |
| `WEB_PASSWORD` | Пароль для дашборда | сложный пароль |
| `DAILY_LOSS_LIMIT_RUB` | Дневной лимит убытков (руб) | `-500.0` |
| `SANDBOX` | Режим песочницы | `true` / `false` |

### instruments.yaml

Каждый инструмент — отдельная секция. Параметры:

| Параметр | По умолчанию | Описание |
|---|---|---|
| `figi` | — | Идентификатор инструмента в T-Invest |
| `lot_size` | — | Количество акций в одном лоте |
| `ofi_threshold` | `0.6` | Минимальный OFI для сигнала (0–1) |
| `print_multiplier` | `7.0` | Во сколько раз объём принта > медианы |
| `print_window` | `200` | Размер окна для расчёта медианы |
| `ofi_levels` | `5` | Уровней стакана для OFI |
| `cooldown_seconds` | `60` | Пауза между сигналами |
| `max_hold_minutes` | `60` | Максимальное время удержания |
| `stop_ticks` | `3` | Стоп-лосс в тиках |
| `max_position_lots` | `1` | Максимальный размер позиции (лотов) |

---

## Как добавить новый инструмент

**Шаг 1**: Найти FIGI тикера

```python
from tinkoff.invest import Client
with Client("YOUR_TOKEN") as client:
    r = client.instruments.find_instrument(query="LKOH")
    for i in r.instruments:
        print(i.ticker, i.figi, i.lot)
```

**Шаг 2**: Добавить секцию в `trading_bot/config/instruments.yaml`

```yaml
LKOH:
  figi: "BBG004731032"
  lot_size: 1
  ofi_threshold: 0.6
  print_multiplier: 6.0
  print_window: 150
  ofi_levels: 5
  cooldown_seconds: 90
  max_position_lots: 1
  trading_hours:
    start: "10:05"
    end: "18:30"
  skip_first_minutes: 5
```

**Шаг 3**: Перезапустить бот. Запись в таблице `instruments` создастся автоматически.

> Изменений кода не требуется.

---

## Как подбирать параметры

### ofi_threshold (0.0 – 1.0)
- **Высокое значение** (0.7–0.9): меньше сигналов, выше качество. Для волатильных дней.
- **Низкое значение** (0.4–0.6): больше сигналов, больше ложных. Для спокойных дней.
- **Метрика**: смотреть на страницу `/signals` — сколько сигналов combo_triggered за день.
  Оптимально: 5–15 сигналов в день.

### print_multiplier (3.0 – 15.0)
- Чем выше, тем реже обнаруживаются принты.
- Начать с 7.0, смотреть на `print_volume` в журнале сигналов.
- Если большинство сигналов без принта — снизить до 5.0.

### print_window (50 – 500)
- Определяет, насколько "свежей" должна быть базовая медиана.
- Маленькое окно (50–100): адаптируется к внутридневным паттернам.
- Большое окно (300–500): более стабильный базовый уровень.

### cooldown_seconds (30 – 300)
- Предотвращает вход на "эхе" первого принта.
- При высокой волатильности увеличить до 120–180 секунд.

### Workflow подбора
1. Запустить бот в `SANDBOX=true`
2. Дать поработать 3–5 торговых дней
3. Открыть `/stats` → смотреть P&L по часам (лучшие часы)
4. Открыть `/signals` → смотреть соотношение acted_on/total
5. Корректировать порог OFI и мультипликатор принта
6. Повторить

---

## Как читать дашборд

### Главная страница (/)

- **P&L сегодня** — реализованная прибыль/убыток за текущий торговый день
- **Win Rate** — процент прибыльных сделок сегодня
- **Открытая позиция** — если есть активная позиция: направление, цена входа,
  нереализованный P&L (обновляется каждые 5 сек)
- **График equity** — накопленная кривая доходности за 30 дней
- **Статус бота** — зелёная точка = торгует, серая = остановлен.
  Кнопка "Остановить" мгновенно запрещает новые ордера (не закрывает текущую позицию!)

### Журнал сделок (/trades)

- **Причина выхода**: `ofi_reversed` — стратегический выход, `timeout` — по времени,
  `stop_loss` — стоп-лосс, `manual` — ручной
- Фильтры позволяют сравнить прибыльность по часам и дням
- Экспорт CSV → Excel для углублённого анализа

### Журнал сигналов (/signals)

Ключевое поле — **Исполнен (acted_on)**:
- `Нет` — риск-менеджер заблокировал, или бот был остановлен
- Смотреть в bot_logs (таблица в БД) причину блокировки

### Статистика (/stats)

- **Profit Factor** > 1.5 = стратегия прибыльна в долгосроке
- **P&L по часу** — находить "золотые часы" торговли
- **P&L по дню недели** — не торговать в убыточные дни

---

## Архитектура

```
main.py                     # Точка входа, оркестрация потоков
├── StreamHandler           # Стрим стакана и сделок
│   └── on_orderbook/on_trade → ComboStrategy → PositionManager
├── ComboStrategy           # OFICalculator + PrintDetector
├── PositionManager         # Открытие/закрытие + P&L
│   └── RiskManager         # Проверки перед ордером
│   └── OrderManager        # T-Invest API
├── APScheduler             # Проверка тайм-аута (1/мин)
└── Flask App               # Веб-дашборд (отдельный поток)
```

### Поток данных
```
T-Invest API стрим
  → StreamHandler.normalize()
  → ComboStrategy.on_orderbook() / on_trade()
      → OFICalculator.update()
      → PrintDetector.on_trade()
      → get_signal() → Signal
  → PositionManager.on_signal()
      → RiskManager.check_all()
      → OrderManager.place_market_order()
      → repository.save_trade()
```
