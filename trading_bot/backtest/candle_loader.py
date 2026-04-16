"""
Загрузка 5-минутных свечей из T-Invest API с дисковым кэшем.
Завершённые дни кэшируются в ~/.cache/invest-bot/candles/ и при повторном
запросе того же периода API не вызывается.
"""
import logging
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, TypedDict

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "invest-bot" / "candles"


class OHLCVCandle(TypedDict):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def load_candles(figi: str, ticker: str, days: int) -> List[OHLCVCandle]:
    """
    Загрузить 5-мин свечи из T-Invest API за последние `days` дней.
    Завершённые дни берутся из кэша; текущий (неполный) день всегда свежий.
    Возвращает список OHLCVCandle, отсортированный по времени без дублей.
    """
    from tinkoff.invest import CandleInterval, Client
    from tinkoff.invest.sandbox.client import SandboxClient
    from tinkoff.invest.utils import quotation_to_decimal
    from trading_bot.config import settings

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    ClientClass = SandboxClient if settings.USE_SANDBOX else Client
    now = datetime.now(timezone.utc)

    all_candles: List[OHLCVCandle] = []

    with ClientClass(settings.TINKOFF_TOKEN) as client:
        for day_offset in range(days - 1, -1, -1):
            chunk_to = now - timedelta(days=day_offset)
            chunk_from = chunk_to - timedelta(days=1)
            date_str = chunk_from.strftime("%Y-%m-%d")
            is_today = day_offset == 0
            cache_file = CACHE_DIR / f"{ticker}_{date_str}.pkl"

            if not is_today and cache_file.exists():
                try:
                    with open(cache_file, "rb") as f:
                        cached = pickle.load(f)
                    all_candles.extend(cached)
                    continue
                except Exception:
                    pass  # fall through to API

            try:
                resp = client.market_data.get_candles(
                    figi=figi,
                    from_=chunk_from,
                    to=chunk_to,
                    interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
                )
                day_candles: List[OHLCVCandle] = [
                    OHLCVCandle(
                        time=c.time,
                        open=float(quotation_to_decimal(c.open)),
                        high=float(quotation_to_decimal(c.high)),
                        low=float(quotation_to_decimal(c.low)),
                        close=float(quotation_to_decimal(c.close)),
                        volume=float(c.volume),
                    )
                    for c in resp.candles
                    if c.is_complete
                ]
                if not is_today and day_candles:
                    with open(cache_file, "wb") as f:
                        pickle.dump(day_candles, f)
                all_candles.extend(day_candles)
            except Exception as e:
                logger.debug(f"Пропуск {date_str} для {ticker}: {e}")

    # Deduplicate and sort
    seen: set = set()
    unique: List[OHLCVCandle] = []
    for c in sorted(all_candles, key=lambda x: x["time"]):
        if c["time"] not in seen:
            seen.add(c["time"])
            unique.append(c)

    logger.info(f"[{ticker}] Загружено {len(unique)} свечей за {days} дней")
    return unique
