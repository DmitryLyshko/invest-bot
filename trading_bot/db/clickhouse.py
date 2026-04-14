"""
ClickHouse клиент для хранения рыночных данных (стакан + тиковые сделки).

Заменяет MySQL-таблицы market_orderbooks и market_trade_ticks.
При CLICKHOUSE_HOST="" — модуль отключён, DataRecorder пишет в MySQL как раньше.

Архитектура:
  - ClickHouseWriter: singleton, держит соединение и два in-memory буфера.
  - Буфер сбрасывается в CH каждые FLUSH_INTERVAL секунд (фоновый поток) или
    при достижении FLUSH_SIZE строк — чтобы не делать один INSERT на каждый тик.
  - При остановке бота atexit гарантирует финальный flush.
"""
import atexit
import json
import logging
import threading
import time
from datetime import datetime
from typing import Generator, List, Optional, Tuple

import clickhouse_connect

from trading_bot.config import settings

logger = logging.getLogger(__name__)

FLUSH_SIZE     = 1_000   # строк в буфере → принудительный flush
FLUSH_INTERVAL = 5.0     # секунд между автоматическими flush

_DDL_ORDERBOOKS = """
CREATE TABLE IF NOT EXISTS market_orderbooks (
    figi        String,
    bids        String,
    asks        String,
    recorded_at DateTime64(3, 'UTC')
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(recorded_at)
ORDER BY (figi, recorded_at)
SETTINGS index_granularity = 8192
"""

_DDL_TRADE_TICKS = """
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
"""


class ClickHouseWriter:
    """
    Буферизованный writer для ClickHouse.

    Накапливает строки в памяти и пишет батчами — ClickHouse
    плохо переносит миллионы мелких INSERT-ов.
    """

    def __init__(self) -> None:
        self._client = clickhouse_connect.get_client(
            host=settings.CLICKHOUSE_HOST,
            port=settings.CLICKHOUSE_PORT,
            username=settings.CLICKHOUSE_USER,
            password=settings.CLICKHOUSE_PASSWORD,
            database=settings.CLICKHOUSE_DATABASE,
            connect_timeout=10,
            send_receive_timeout=30,
        )
        self._ob_buf:   list = []   # [figi, bids_json, asks_json, ts]
        self._tick_buf: list = []   # [figi, price, qty, direction, ts]
        self._lock = threading.Lock()

        self._init_tables()
        self._start_flush_thread()
        atexit.register(self.flush)
        logger.info(
            "ClickHouse подключён: %s:%s/%s",
            settings.CLICKHOUSE_HOST,
            settings.CLICKHOUSE_PORT,
            settings.CLICKHOUSE_DATABASE,
        )

    def _init_tables(self) -> None:
        self._client.command(_DDL_ORDERBOOKS)
        self._client.command(_DDL_TRADE_TICKS)
        logger.debug("ClickHouse: таблицы проверены/созданы")

    def _start_flush_thread(self) -> None:
        t = threading.Thread(target=self._flush_loop, daemon=True, name="ch_flush")
        t.start()

    def _flush_loop(self) -> None:
        while True:
            time.sleep(FLUSH_INTERVAL)
            try:
                self.flush()
            except Exception as exc:
                logger.error("ClickHouse flush error: %s", exc)

    # ── Запись ─────────────────────────────────────────────────────────────────

    def insert_orderbook(
        self,
        figi: str,
        bids: list,
        asks: list,
        timestamp: datetime,
    ) -> None:
        row = [figi, json.dumps(bids), json.dumps(asks), timestamp]
        with self._lock:
            self._ob_buf.append(row)
            should_flush = len(self._ob_buf) >= FLUSH_SIZE
        if should_flush:
            self.flush()

    def insert_trade_tick(
        self,
        figi: str,
        price: float,
        quantity: int,
        direction: str,
        timestamp: datetime,
    ) -> None:
        row = [figi, price, quantity, direction, timestamp]
        with self._lock:
            self._tick_buf.append(row)
            should_flush = len(self._tick_buf) >= FLUSH_SIZE
        if should_flush:
            self.flush()

    def flush(self) -> None:
        """Сбросить оба буфера в ClickHouse."""
        with self._lock:
            ob_batch   = self._ob_buf[:]
            tick_batch = self._tick_buf[:]
            self._ob_buf   = []
            self._tick_buf = []

        if ob_batch:
            try:
                self._client.insert(
                    "market_orderbooks",
                    ob_batch,
                    column_names=["figi", "bids", "asks", "recorded_at"],
                )
                logger.debug("ClickHouse: записано %d стаканов", len(ob_batch))
            except Exception as exc:
                logger.error("ClickHouse: ошибка записи стаканов: %s", exc)
                with self._lock:
                    self._ob_buf = ob_batch + self._ob_buf  # вернуть в буфер

        if tick_batch:
            try:
                self._client.insert(
                    "market_trade_ticks",
                    tick_batch,
                    column_names=["figi", "price", "quantity", "direction", "recorded_at"],
                )
                logger.debug("ClickHouse: записано %d тиков", len(tick_batch))
            except Exception as exc:
                logger.error("ClickHouse: ошибка записи тиков: %s", exc)
                with self._lock:
                    self._tick_buf = tick_batch + self._tick_buf

    # ── Чтение ─────────────────────────────────────────────────────────────────

    def query_orderbooks(
        self,
        figi: str,
        date_from: datetime,
        date_to: datetime,
    ) -> List[Tuple[datetime, str, str]]:
        result = self._client.query(
            "SELECT recorded_at, bids, asks FROM market_orderbooks "
            "WHERE figi = {figi:String} "
            "  AND recorded_at >= {from:DateTime64} "
            "  AND recorded_at <  {to:DateTime64} "
            "ORDER BY recorded_at",
            parameters={"figi": figi, "from": date_from, "to": date_to},
        )
        return result.result_rows

    def iter_orderbooks(
        self,
        figi: str,
        date_from: datetime,
        date_to: datetime,
        chunk_size: int = 2_000,
    ) -> Generator[Tuple[datetime, str, str], None, None]:
        offset = 0
        while True:
            result = self._client.query(
                "SELECT recorded_at, bids, asks FROM market_orderbooks "
                "WHERE figi = {figi:String} "
                "  AND recorded_at >= {from:DateTime64} "
                "  AND recorded_at <  {to:DateTime64} "
                "ORDER BY recorded_at "
                "LIMIT {chunk:Int32} OFFSET {offset:Int32}",
                parameters={
                    "figi": figi, "from": date_from, "to": date_to,
                    "chunk": chunk_size, "offset": offset,
                },
            )
            rows = result.result_rows
            if not rows:
                break
            yield from rows
            if len(rows) < chunk_size:
                break
            offset += chunk_size

    def query_trade_ticks(
        self,
        figi: str,
        date_from: datetime,
        date_to: datetime,
    ) -> List[Tuple[datetime, float, int, str]]:
        result = self._client.query(
            "SELECT recorded_at, price, quantity, direction FROM market_trade_ticks "
            "WHERE figi = {figi:String} "
            "  AND recorded_at >= {from:DateTime64} "
            "  AND recorded_at <  {to:DateTime64} "
            "ORDER BY recorded_at",
            parameters={"figi": figi, "from": date_from, "to": date_to},
        )
        return result.result_rows

    def iter_trade_ticks(
        self,
        figi: str,
        date_from: datetime,
        date_to: datetime,
        chunk_size: int = 5_000,
    ) -> Generator[Tuple[datetime, float, int, str], None, None]:
        offset = 0
        while True:
            result = self._client.query(
                "SELECT recorded_at, price, quantity, direction FROM market_trade_ticks "
                "WHERE figi = {figi:String} "
                "  AND recorded_at >= {from:DateTime64} "
                "  AND recorded_at <  {to:DateTime64} "
                "ORDER BY recorded_at "
                "LIMIT {chunk:Int32} OFFSET {offset:Int32}",
                parameters={
                    "figi": figi, "from": date_from, "to": date_to,
                    "chunk": chunk_size, "offset": offset,
                },
            )
            rows = result.result_rows
            if not rows:
                break
            yield from rows
            if len(rows) < chunk_size:
                break
            offset += chunk_size

    def query_recorded_dates(self, figi: str) -> List[str]:
        result = self._client.query(
            "SELECT DISTINCT toDate(recorded_at) AS d "
            "FROM market_orderbooks "
            "WHERE figi = {figi:String} "
            "ORDER BY d",
            parameters={"figi": figi},
        )
        return [str(row[0]) for row in result.result_rows]

    def count_orderbooks(self, figi: Optional[str] = None) -> int:
        where = "WHERE figi = {figi:String}" if figi else ""
        params = {"figi": figi} if figi else {}
        result = self._client.query(
            f"SELECT count() FROM market_orderbooks {where}",
            parameters=params,
        )
        return result.result_rows[0][0]

    def count_trade_ticks(self, figi: Optional[str] = None) -> int:
        where = "WHERE figi = {figi:String}" if figi else ""
        params = {"figi": figi} if figi else {}
        result = self._client.query(
            f"SELECT count() FROM market_trade_ticks {where}",
            parameters=params,
        )
        return result.result_rows[0][0]


# ── Singleton ───────────────────────────────────────────────────────────────────

_writer: Optional[ClickHouseWriter] = None
_writer_lock = threading.Lock()


def is_enabled() -> bool:
    """ClickHouse включён если задан CLICKHOUSE_HOST."""
    return bool(settings.CLICKHOUSE_HOST)


def get_writer() -> ClickHouseWriter:
    """Получить singleton ClickHouseWriter. Вызывать только если is_enabled()."""
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = ClickHouseWriter()
        return _writer


def init_clickhouse() -> None:
    """Инициализировать соединение при старте бота (если CH включён)."""
    if is_enabled():
        get_writer()
