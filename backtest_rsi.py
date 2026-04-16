#!/usr/bin/env python3
"""
CLI бэктест RSI-стратегии на исторических 5-мин свечах из T-Invest API.

Использование:
  python backtest_rsi.py --ticker SBER --days 60
  python backtest_rsi.py --all --days 90
  python backtest_rsi.py --all --days 60 --no-cache
"""
import argparse
import sys
from pathlib import Path

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent))


def _load_configs():
    import yaml
    from trading_bot.config import settings

    rsi_path = settings.INSTRUMENTS_CONFIG_PATH.parent / "rsi_config.yaml"
    instr_path = settings.INSTRUMENTS_CONFIG_PATH

    with open(rsi_path, "r", encoding="utf-8") as f:
        rsi_cfg = yaml.safe_load(f) or {}
    with open(instr_path, "r", encoding="utf-8") as f:
        instr_cfg = yaml.safe_load(f) or {}
    return rsi_cfg, instr_cfg


def run_ticker(ticker: str, days: int, no_cache: bool) -> dict:
    from trading_bot.backtest.candle_loader import load_candles, CACHE_DIR
    from trading_bot.backtest.engine import run_backtest

    rsi_cfg, instr_cfg = _load_configs()

    if ticker not in rsi_cfg:
        print(f"  ✗ {ticker} не найден в rsi_config.yaml")
        return {}
    if ticker not in instr_cfg:
        print(f"  ✗ {ticker} не найден в instruments.yaml")
        return {}

    if no_cache:
        import shutil
        for f in CACHE_DIR.glob(f"{ticker}_*.pkl"):
            f.unlink()

    rsi_params = rsi_cfg[ticker]
    instr = instr_cfg[ticker]
    instr_params = {
        "ticker": ticker,
        "lot_size": instr.get("lot_size", 1),
        "tick_size": instr.get("tick_size", 0.01),
        "commission_rate": instr.get("commission_rate", 0.0004),
    }
    warmup = rsi_params.get("warmup_candles", 300)

    print(f"  → Загружаем свечи {ticker} за {days} дней...")
    candles = load_candles(figi=instr["figi"], ticker=ticker, days=days)
    print(f"  → {len(candles)} свечей, прогрев {warmup}, запускаем движок...")

    result = run_backtest(
        candles=candles,
        rsi_params=rsi_params,
        instrument_params=instr_params,
        warmup_candles=warmup,
        days=days,
    )
    return result


def print_result(result: dict) -> None:
    if not result:
        return
    m = result["metrics"]
    ticker = result["ticker"]
    pnl_sign = "+" if m["total_pnl"] >= 0 else ""
    pf_str = f"{m['profit_factor']:.2f}"
    ok = "✓" if m["profit_factor"] >= 1 and m["n_trades"] >= 5 else "✗"
    reasons = ", ".join(f"{k}:{v}" for k, v in (m.get("exit_reasons") or {}).items())

    print(f"\n{'─'*60}")
    print(f"  {ticker}  [{result['days']}д | {result['candles_used']} свечей]  {ok}")
    print(f"  Сделок:       {m['n_trades']}")
    print(f"  Win rate:     {m['win_rate']:.1f}%")
    print(f"  P&L/лот:      {pnl_sign}{m['total_pnl']:.2f} ₽")
    print(f"  Profit Factor:{pf_str}")
    print(f"  Max Drawdown: -{m['max_drawdown']:.2f} ₽")
    print(f"  Avg hold:     {m['avg_hold_candles']:.1f} свечей")
    if reasons:
        print(f"  Причины:      {reasons}")


def print_summary_table(results: list) -> None:
    print(f"\n{'═'*80}")
    print(f"{'ТИКЕР':<8} {'Сделок':>7} {'Win%':>6} {'PnL/лот':>10} {'PF':>6} {'MaxDD':>9} {'AvgHold':>8} {'OK':>4}")
    print(f"{'─'*80}")
    for r in sorted(results, key=lambda x: x.get("metrics", {}).get("profit_factor", 0), reverse=True):
        if not r or "error" in r:
            print(f"  {r.get('ticker','?'):<8} ERROR: {r.get('error','')}")
            continue
        m = r["metrics"]
        ok = "✓" if m["profit_factor"] >= 1 and m["n_trades"] >= 5 else "✗"
        pnl_s = f"{'+' if m['total_pnl'] >= 0 else ''}{m['total_pnl']:.1f}"
        print(
            f"  {r['ticker']:<6} {m['n_trades']:>7} {m['win_rate']:>5.1f}%"
            f" {pnl_s:>10} {m['profit_factor']:>6.2f}"
            f" {m['max_drawdown']:>9.1f} {m['avg_hold_candles']:>7.1f}  {ok}"
        )
    print(f"{'═'*80}\n")


def main():
    parser = argparse.ArgumentParser(description="Бэктест RSI-стратегии")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", metavar="TICKER", help="Тикер (напр. SBER)")
    group.add_argument("--all", action="store_true", help="Все тикеры из rsi_config.yaml")
    parser.add_argument("--days", type=int, default=60, help="Период в днях (7-180)")
    parser.add_argument("--no-cache", action="store_true", help="Сбросить кэш свечей")
    args = parser.parse_args()

    days = max(7, min(180, args.days))

    if args.ticker:
        ticker = args.ticker.upper()
        print(f"\nБэктест {ticker} за {days} дней")
        try:
            result = run_ticker(ticker, days, args.no_cache)
            print_result(result)
        except Exception as e:
            print(f"  ✗ Ошибка: {e}")
            sys.exit(1)
    else:
        rsi_cfg, _ = _load_configs()
        tickers = sorted(rsi_cfg.keys())
        print(f"\nБэктест всех тикеров ({len(tickers)}) за {days} дней")
        results = []
        for t in tickers:
            print(f"\n[{tickers.index(t)+1}/{len(tickers)}] {t}")
            try:
                r = run_ticker(t, days, args.no_cache)
                results.append(r)
            except Exception as e:
                print(f"  ✗ Ошибка: {e}")
                results.append({"ticker": t, "error": str(e)})
        print_summary_table([r for r in results if r])


if __name__ == "__main__":
    main()
