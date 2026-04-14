#!/usr/bin/env python3
"""
Автоматическая калибровка print_multiplier в instruments.yaml
на основе данных market_trade_ticks из ClickHouse за последние LOOKBACK_DAYS дней.

Формула: new_multiplier = clamp(round((p95/median + p99/median) / 2), MIN, MAX)
Целевой уровень: ~p97 — принтом считается только ~2-3% сделок по объёму.

Обновляет yaml точечно (построчно) — комментарии и форматирование не трогает.
Рестарт бота после обновления — задача crontab.

Crontab:
  0 1 * * * cd /opt/invest-bot && .venv/bin/python calibrate_multipliers.py >> /var/log/invest-bot/calibrate.log 2>&1
  5 1 * * * systemctl restart trading-bot
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clickhouse_connect
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from trading_bot.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

LOOKBACK_DAYS    = 10
MIN_TRADES       = 200    # меньше — недостаточно данных для надёжной калибровки
CHANGE_THRESHOLD = 0.20   # 20% изменение → обновить
MULTIPLIER_MIN   = 10
MULTIPLIER_MAX   = 200


# ── Калибровка ────────────────────────────────────────────────────────────────

def compute_multiplier(p95_ratio: float, p99_ratio: float) -> int:
    """Целевой уровень ~p97: среднее между p95 и p99 соотношениями к медиане."""
    raw = (p95_ratio + p99_ratio) / 2
    return int(max(MULTIPLIER_MIN, min(MULTIPLIER_MAX, round(raw))))


# ── ClickHouse ────────────────────────────────────────────────────────────────

def query_stats(client, date_from: datetime) -> dict[str, dict]:
    """Возвращает {figi: {trades, median, p95_ratio, p99_ratio}}."""
    result = client.query(
        """
        SELECT
            figi,
            count()                                          AS trades,
            median(quantity)                                 AS median_qty,
            quantile(0.95)(quantity) / median(quantity)     AS p95_ratio,
            quantile(0.99)(quantity) / median(quantity)     AS p99_ratio
        FROM market_trade_ticks
        WHERE recorded_at >= {from:DateTime64}
          AND quantity > 0
        GROUP BY figi
        HAVING count()          >= {min_trades:UInt32}
           AND median(quantity) >  0
        """,
        parameters={"from": date_from, "min_trades": MIN_TRADES},
    )
    return {
        row[0]: {
            "trades":    int(row[1]),
            "median":    float(row[2]),
            "p95_ratio": float(row[3]),
            "p99_ratio": float(row[4]),
        }
        for row in result.result_rows
    }


# ── instruments.yaml ──────────────────────────────────────────────────────────

def load_figi_map(path: Path) -> dict[str, str]:
    """Возвращает {figi: ticker}. Читаем вручную чтобы не зависеть от yaml-парсера."""
    import yaml
    with open(path) as f:
        config = yaml.safe_load(f)
    return {v["figi"]: k for k, v in config.items() if isinstance(v, dict) and "figi" in v}


def load_current_multipliers(path: Path) -> dict[str, float]:
    """Возвращает {ticker: print_multiplier}."""
    import yaml
    with open(path) as f:
        config = yaml.safe_load(f)
    return {
        k: float(v.get("print_multiplier", 50.0))
        for k, v in config.items()
        if isinstance(v, dict)
    }


def apply_updates(path: Path, updates: dict[str, float]) -> None:
    """
    Точечно обновляет print_multiplier в yaml-файле.
    Алгоритм: идём по строкам, отслеживаем текущий тикер (ключ верхнего уровня),
    при нахождении строки print_multiplier: заменяем значение.
    Комментарии, отступы и порядок ключей не изменяются.
    """
    lines = path.read_text().splitlines(keepends=True)
    current_ticker: str | None = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # Ключ верхнего уровня (без отступа, не комментарий, не trading_hours)
        if line and not line[0].isspace() and not line.startswith("#") and ":" in line:
            current_ticker = line.split(":")[0].strip()

        if current_ticker in updates and stripped.startswith("print_multiplier:"):
            indent = len(line) - len(stripped)
            new_val = updates[current_ticker]
            lines[i] = " " * indent + f"print_multiplier: {new_val:.1f}\n"

    path.write_text("".join(lines))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not settings.CLICKHOUSE_HOST:
        logger.error("CLICKHOUSE_HOST не задан — выход")
        sys.exit(1)

    client = clickhouse_connect.get_client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        username=settings.CLICKHOUSE_USER,
        password=settings.CLICKHOUSE_PASSWORD,
        database=settings.CLICKHOUSE_DATABASE,
        connect_timeout=10,
        send_receive_timeout=60,
    )
    logger.info(
        "ClickHouse: %s:%s/%s",
        settings.CLICKHOUSE_HOST, settings.CLICKHOUSE_PORT, settings.CLICKHOUSE_DATABASE,
    )

    date_from = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    stats     = query_stats(client, date_from)
    logger.info("Данные получены: %d figi за последние %d дней", len(stats), LOOKBACK_DAYS)

    yaml_path        = settings.INSTRUMENTS_CONFIG_PATH
    figi_to_ticker   = load_figi_map(yaml_path)
    current_mults    = load_current_multipliers(yaml_path)

    updates:      dict[str, float] = {}
    no_data:      list[str]        = []

    for ticker, current in current_mults.items():
        # найти figi для этого тикера
        figi = next((f for f, t in figi_to_ticker.items() if t == ticker), None)
        if figi is None or figi not in stats:
            no_data.append(ticker)
            continue

        s        = stats[figi]
        new_mult = compute_multiplier(s["p95_ratio"], s["p99_ratio"])
        delta    = abs(new_mult - current) / current if current else 1.0

        logger.info(
            "%-6s  текущий=%5.1f  новый=%3d  "
            "p95_ratio=%6.1f  p99_ratio=%7.1f  "
            "median=%.0f  trades=%d  delta=%+.0f%%",
            ticker, current, new_mult,
            s["p95_ratio"], s["p99_ratio"],
            s["median"], s["trades"],
            (new_mult - current) / current * 100,
        )

        if delta >= CHANGE_THRESHOLD:
            updates[ticker] = float(new_mult)

    if updates:
        apply_updates(yaml_path, updates)
        logger.info("instruments.yaml обновлён (%d тикеров):", len(updates))
        for ticker, new_val in updates.items():
            logger.info("  %-6s  %.1f → %.1f", ticker, current_mults[ticker], new_val)
    else:
        logger.info("Изменений нет (все delta < %.0f%%) — файл не тронут", CHANGE_THRESHOLD * 100)

    if no_data:
        logger.warning(
            "Пропущены (< %d сделок за %d дней): %s",
            MIN_TRADES, LOOKBACK_DAYS, ", ".join(no_data),
        )


if __name__ == "__main__":
    main()
