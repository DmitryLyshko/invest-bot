"""
Миграция market_orderbooks и market_trade_ticks из MySQL в ClickHouse.

Алгоритм (для каждой таблицы):
  1. Читаем строки из MySQL батчами по BATCH_SIZE.
  2. Вставляем батч в ClickHouse.
  3. Удаляем перенесённые строки из MySQL (по id).
  4. Повторяем до исчерпания данных.

Запуск:
    python migrate_to_clickhouse.py

Опции:
    --dry-run     Показать статистику без реальной миграции.
    --batch N     Размер батча (по умолчанию 10000).
    --ticks-only  Мигрировать только trade_ticks.
    --ob-only     Мигрировать только orderbooks.

Важно:
  - Остановите бот перед запуском (или убедитесь RECORD_MARKET_DATA=false).
  - В .env должны быть заданы MYSQL_URL, CLICKHOUSE_HOST и пр.
  - Скрипт идемпотентен: можно запускать повторно при сбое.
"""
import argparse
import json
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text  # noqa: E402
from trading_bot.config import settings  # noqa: E402  (после load_dotenv)


def _check_config() -> None:
    if not settings.CLICKHOUSE_HOST:
        print("ERROR: CLICKHOUSE_HOST не задан в .env")
        sys.exit(1)


def _get_mysql_engine():
    from sqlalchemy import create_engine
    return create_engine(
        settings.MYSQL_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
    )


def _get_ch_client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        username=settings.CLICKHOUSE_USER,
        password=settings.CLICKHOUSE_PASSWORD,
        database=settings.CLICKHOUSE_DATABASE,
        connect_timeout=10,
        send_receive_timeout=60,
    )


def _ensure_ch_tables(ch) -> None:
    ch.command("""
        CREATE TABLE IF NOT EXISTS market_orderbooks (
            figi        String,
            bids        String,
            asks        String,
            recorded_at DateTime64(3, 'UTC')
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(recorded_at)
        ORDER BY (figi, recorded_at)
        SETTINGS index_granularity = 8192
    """)
    ch.command("""
        CREATE TABLE IF NOT EXISTS market_trade_ticks (
            figi        String,
            price       Float64,
            quantity    Int32,
            direction   String,
            recorded_at DateTime64(3, 'UTC')
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(recorded_at)
        ORDER BY (figi, recorded_at)
        SETTINGS index_granularity = 8192
    """)
    print("ClickHouse: таблицы проверены/созданы.")


def _count_mysql(conn, table: str) -> int:
    result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
    return result.scalar()


def _count_ch(ch, table: str) -> int:
    result = ch.query(f"SELECT count() FROM {table}")
    return result.result_rows[0][0]


def _print_stats(conn, ch) -> None:
    print("\n── Текущее состояние ──────────────────────────────────")
    for table in ("market_orderbooks", "market_trade_ticks"):
        try:
            mysql_n = _count_mysql(conn, table)
        except Exception:
            mysql_n = "?"
        try:
            ch_n = _count_ch(ch, table)
        except Exception:
            ch_n = "?"
        print(f"  {table}:")
        print(f"    MySQL:      {mysql_n:>12}")
        print(f"    ClickHouse: {ch_n:>12}")
    print()


def migrate_orderbooks(conn, ch, batch_size: int, dry_run: bool) -> None:
    print("── Миграция market_orderbooks ─────────────────────────")
    total_migrated = 0
    t0 = time.time()

    while True:
        rows = conn.execute(text(
            f"SELECT id, figi, bids, asks, recorded_at "
            f"FROM market_orderbooks ORDER BY id LIMIT {batch_size}"
        )).fetchall()

        if not rows:
            break

        ids = [r[0] for r in rows]
        ch_rows = [
            [r[1], r[2], r[3], r[4]]   # figi, bids, asks, recorded_at
            for r in rows
        ]

        if not dry_run:
            ch.insert(
                "market_orderbooks",
                ch_rows,
                column_names=["figi", "bids", "asks", "recorded_at"],
            )
            ids_str = ",".join(str(i) for i in ids)
            conn.execute(text(f"DELETE FROM market_orderbooks WHERE id IN ({ids_str})"))

        total_migrated += len(rows)
        elapsed = time.time() - t0
        rps = total_migrated / elapsed if elapsed > 0 else 0
        print(
            f"  перенесено {total_migrated:>10} строк"
            f"  [{rps:,.0f} строк/с]"
            + ("  [dry-run]" if dry_run else ""),
            end="\r",
        )

        if dry_run:
            break   # dry-run: показываем только один батч

    print(f"\n  Итого orderbooks: {total_migrated:,} строк за {time.time()-t0:.1f} с")


def migrate_trade_ticks(conn, ch, batch_size: int, dry_run: bool) -> None:
    print("── Миграция market_trade_ticks ────────────────────────")
    total_migrated = 0
    t0 = time.time()

    while True:
        rows = conn.execute(text(
            f"SELECT id, figi, price, quantity, direction, recorded_at "
            f"FROM market_trade_ticks ORDER BY id LIMIT {batch_size}"
        )).fetchall()

        if not rows:
            break

        ids = [r[0] for r in rows]
        ch_rows = [
            [r[1], float(r[2]), int(r[3]), r[4], r[5]]  # figi, price, qty, dir, ts
            for r in rows
        ]

        if not dry_run:
            ch.insert(
                "market_trade_ticks",
                ch_rows,
                column_names=["figi", "price", "quantity", "direction", "recorded_at"],
            )
            ids_str = ",".join(str(i) for i in ids)
            conn.execute(text(f"DELETE FROM market_trade_ticks WHERE id IN ({ids_str})"))

        total_migrated += len(rows)
        elapsed = time.time() - t0
        rps = total_migrated / elapsed if elapsed > 0 else 0
        print(
            f"  перенесено {total_migrated:>10} строк"
            f"  [{rps:,.0f} строк/с]"
            + ("  [dry-run]" if dry_run else ""),
            end="\r",
        )

        if dry_run:
            break

    print(f"\n  Итого trade_ticks: {total_migrated:,} строк за {time.time()-t0:.1f} с")


def main() -> None:
    parser = argparse.ArgumentParser(description="Миграция маркет-данных MySQL → ClickHouse")
    parser.add_argument("--dry-run", action="store_true", help="Только статистика, без изменений")
    parser.add_argument("--batch", type=int, default=10_000, help="Размер батча (default: 10000)")
    parser.add_argument("--ticks-only", action="store_true", help="Только trade_ticks")
    parser.add_argument("--ob-only", action="store_true", help="Только orderbooks")
    args = parser.parse_args()

    _check_config()

    print(f"MySQL:      {settings.MYSQL_URL.split('@')[-1]}")
    print(f"ClickHouse: {settings.CLICKHOUSE_HOST}:{settings.CLICKHOUSE_PORT}/{settings.CLICKHOUSE_DATABASE}")
    if args.dry_run:
        print("Режим: DRY-RUN (данные не изменяются)\n")
    else:
        print(f"Размер батча: {args.batch:,}\n")

    engine = _get_mysql_engine()
    ch = _get_ch_client()

    _ensure_ch_tables(ch)

    with engine.connect() as conn:
        _print_stats(conn, ch)

        if args.dry_run:
            print("Dry-run: показываю первый батч каждой таблицы...\n")

        if not args.ticks_only:
            migrate_orderbooks(conn, ch, args.batch, args.dry_run)
        if not args.ob_only:
            migrate_trade_ticks(conn, ch, args.batch, args.dry_run)

        if not args.dry_run:
            print("\n── Итоговое состояние ─────────────────────────────────")
            _print_stats(conn, ch)
            print("Миграция завершена.")
        else:
            print("\nDry-run завершён. Для реальной миграции запустите без --dry-run.")


if __name__ == "__main__":
    main()
