#!/usr/bin/env python3
"""
Автокалибровка параметров стратегии на основе текущей волатильности.

Для каждого инструмента:
  - Получает 1-мин свечи за последние 7 дней из T-Invest API
  - Рассчитывает ATR(14) как меру волатильности
  - Предлагает stop_ticks  = round(ATR * ATR_MULT / tick_size)
  - Предлагает breakeven_ticks = round(stop_ticks * BREAKEVEN_RATIO)
  - Предлагает take_profit_ticks = stop_ticks * TP_RATIO  (1:3 R:R)
  - Если есть данные market_trade_ticks в БД — калибрует print_multiplier

Использование:
  python calibrate.py            — показать предложения, ничего не менять
  python calibrate.py --apply    — применить изменения в instruments.yaml

Предупреждение: --apply перезаписывает instruments.yaml без комментариев.
Сделайте git commit перед применением.
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml

# ─── Настраиваемые константы ──────────────────────────────────────────────────

ATR_PERIOD = 14          # период ATR (в 1-мин свечах)
ATR_MULT = 1.5           # stop = ATR * ATR_MULT
BREAKEVEN_RATIO = 0.85   # breakeven = stop * BREAKEVEN_RATIO
TP_RATIO = 3             # take_profit = stop * TP_RATIO  (1:3 risk/reward)
CANDLE_DAYS = 7          # сколько календарных дней свечей тянуть
TARGET_PRINTS_PER_DAY = 10  # желаемое кол-во крупных принтов в день (для print_multiplier)
DB_HISTORY_DAYS = 5      # сколько дней market_trade_ticks использовать для print_multiplier


# ─── Загрузка окружения ────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from trading_bot.config import settings

INSTRUMENTS_PATH = settings.INSTRUMENTS_CONFIG_PATH


# ─── Свечи и ATR ──────────────────────────────────────────────────────────────

def fetch_candles(client, instrument_id: str, days: int) -> list:
    """Получить 1-мин свечи за последние N дней."""
    from tinkoff.invest import CandleInterval
    now = datetime.now(timezone.utc)
    response = client.market_data.get_candles(
        instrument_id=instrument_id,
        from_=now - timedelta(days=days),
        to=now,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    return response.candles


def calc_atr(candles, period: int = ATR_PERIOD) -> Optional[float]:
    """Рассчитать ATR(period) по 1-мин свечам."""
    from tinkoff.invest.utils import quotation_to_decimal
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high = float(quotation_to_decimal(candles[i].high))
        low  = float(quotation_to_decimal(candles[i].low))
        prev_close = float(quotation_to_decimal(candles[i - 1].close))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period


def suggest_stops(atr: float, tick_size: float) -> dict:
    """Предложить stop/breakeven/take_profit по ATR."""
    stop = max(1, round(atr * ATR_MULT / tick_size))
    breakeven = max(1, round(stop * BREAKEVEN_RATIO))
    take_profit = stop * TP_RATIO
    return {
        "stop_ticks": stop,
        "breakeven_ticks": breakeven,
        "take_profit_ticks": take_profit,
    }


# ─── Калибровка print_multiplier ──────────────────────────────────────────────

def suggest_print_multiplier(figi: str) -> Optional[float]:
    """
    Предложить print_multiplier на основе данных market_trade_ticks в БД.

    Ищет мультипликатор при котором в среднем TARGET_PRINTS_PER_DAY
    сделок в день превышают медиану * multiplier.

    Возвращает None если данных недостаточно.
    """
    try:
        from trading_bot.db.repository import get_session
        from trading_bot.db.models import MarketTradeTick
        from sqlalchemy import func

        cutoff = datetime.utcnow() - timedelta(days=DB_HISTORY_DAYS)

        with get_session() as session:
            total = session.query(func.count(MarketTradeTick.id)).filter(
                MarketTradeTick.figi == figi,
                MarketTradeTick.recorded_at >= cutoff,
            ).scalar() or 0

            if total < 200:
                return None

            volumes = [
                row[0] for row in
                session.query(MarketTradeTick.quantity).filter(
                    MarketTradeTick.figi == figi,
                    MarketTradeTick.recorded_at >= cutoff,
                ).all()
            ]

        volumes_sorted = sorted(volumes)
        n = len(volumes_sorted)
        median = volumes_sorted[n // 2] if n % 2 else (volumes_sorted[n // 2 - 1] + volumes_sorted[n // 2]) / 2

        if median <= 0:
            return None

        target_total = TARGET_PRINTS_PER_DAY * DB_HISTORY_DAYS

        # Бинарный поиск мультипликатора
        lo, hi = 1.0, 100.0
        for _ in range(30):
            mid = (lo + hi) / 2
            count_prints = sum(1 for v in volumes if v >= median * mid)
            if count_prints > target_total:
                lo = mid
            else:
                hi = mid

        return round((lo + hi) / 2, 1)

    except Exception as e:
        print(f"    (print_multiplier: ошибка БД — {e})")
        return None


# ─── Вывод и применение ───────────────────────────────────────────────────────

def fmt_delta(current, suggested) -> str:
    if isinstance(current, float) or isinstance(suggested, float):
        d = suggested - current
        return f"{d:+.1f}" if abs(d) > 0.05 else "—"
    d = suggested - current
    return f"{d:+d}" if d != 0 else "—"


def print_table(ticker: str, atr: float, tick_size: float,
                current: dict, suggested: dict) -> None:
    print(f"\n{'─'*60}")
    print(f"  {ticker}   ATR(14) 1-мин = {atr:.5f} руб  "
          f"(в тиках: {atr / tick_size:.1f})")
    print(f"  {'Параметр':<28} {'Текущее':>9} {'Предложено':>11} {'Δ':>8}")
    print(f"  {'─'*56}")
    keys = ["stop_ticks", "breakeven_ticks", "take_profit_ticks", "print_multiplier"]
    for k in keys:
        if k not in suggested:
            continue
        cur = current.get(k, "—")
        sug = suggested[k]
        delta = fmt_delta(cur, sug) if cur != "—" else "новый"
        changed = "  ←" if delta not in ("—", "") else ""
        if isinstance(sug, float):
            print(f"  {k:<28} {str(cur):>9} {sug:>11.1f} {delta:>8}{changed}")
        else:
            print(f"  {k:<28} {str(cur):>9} {sug:>11} {delta:>8}{changed}")


def apply_changes(config: dict, all_suggested: dict) -> None:
    """Применить предложенные изменения и перезаписать instruments.yaml."""
    for ticker, suggested in all_suggested.items():
        config[ticker].update(suggested)
    with open(INSTRUMENTS_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, indent=2)
    print(f"\n✓ {INSTRUMENTS_PATH} обновлён")
    print("  Перезапустите бот для применения новых параметров.")


# ─── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    apply = "--apply" in sys.argv

    if apply:
        print("⚠  Режим --apply: instruments.yaml будет перезаписан без комментариев.")
        print("   Рекомендуется сделать git commit перед продолжением.")
        answer = input("   Продолжить? [y/N] ").strip().lower()
        if answer != "y":
            print("Отменено.")
            return

    with open(INSTRUMENTS_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Для получения свечей всегда используем реальный клиент, не sandbox
    from tinkoff.invest import Client
    token = settings.TINKOFF_MARKET_TOKEN or settings.TINKOFF_TOKEN

    all_suggested: dict[str, dict] = {}

    print(f"\nКалибровка параметров — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"ATR_MULT={ATR_MULT}  BREAKEVEN_RATIO={BREAKEVEN_RATIO}  TP_RATIO=1:{TP_RATIO}  "
          f"Свечи: {CANDLE_DAYS} дней")

    with Client(token) as client:
        for ticker, params in config.items():
            instrument_id = params.get("instrument_id", "")
            tick_size = params.get("tick_size", 0.01)
            figi = params["figi"]

            if not instrument_id:
                print(f"\n{ticker}: нет instrument_id, пропускаем")
                continue

            # ── Свечи и ATR ───────────────────────────────────────────────────
            try:
                candles = fetch_candles(client, instrument_id, CANDLE_DAYS)
            except Exception as e:
                print(f"\n{ticker}: ошибка получения свечей — {e}")
                continue

            if len(candles) < ATR_PERIOD + 1:
                print(f"\n{ticker}: недостаточно свечей ({len(candles)}), пропускаем")
                continue

            atr = calc_atr(candles)
            if atr is None or atr == 0:
                print(f"\n{ticker}: ATR=0, пропускаем")
                continue

            suggested = suggest_stops(atr, tick_size)

            # ── print_multiplier из БД ────────────────────────────────────────
            pm = suggest_print_multiplier(figi)
            if pm is not None:
                suggested["print_multiplier"] = pm

            # ── Фильтр: пропускаем если изменений нет ────────────────────────
            changes = {
                k: v for k, v in suggested.items()
                if abs(v - params.get(k, 0)) > (0.05 if isinstance(v, float) else 0)
            }

            print_table(ticker, atr, tick_size, params, suggested)

            if not changes:
                print("  Изменений нет — параметры актуальны.")
            else:
                all_suggested[ticker] = changes

    # ── Итог ──────────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    if not all_suggested:
        print("Все параметры актуальны. Изменений нет.")
        return

    tickers_changed = ", ".join(all_suggested)
    print(f"Предложены изменения для: {tickers_changed}")

    if apply:
        apply_changes(config, all_suggested)
    else:
        print("\nДля применения запустите:")
        print("  python calibrate.py --apply")


if __name__ == "__main__":
    main()
