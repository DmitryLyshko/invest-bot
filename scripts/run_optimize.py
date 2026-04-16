"""
Запуск оптимизатора для всех (или указанных) тикеров.
Результат сохраняется в result.csv.

Использование:
    python scripts/run_optimize.py                        # все тикеры
    python scripts/run_optimize.py SBER GAZP              # конкретные
    python scripts/run_optimize.py --days 60 --min-trades 30
    python scripts/run_optimize.py --out my_result.csv
"""
import argparse
import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from trading_bot.backtest.candle_loader import load_candles
from trading_bot.backtest.engine import run_backtest
from trading_bot.backtest.optimizer import optimize_ticker
from trading_bot.config import settings

RSI_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH.parent / "rsi_config.yaml"
INSTRUMENTS_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH

CSV_FIELDS = [
    "ticker",
    "rank",
    "profit_factor",
    "total_pnl",
    "n_trades",
    "win_rate",
    "ob_value",
    "os_value",
    "stop_ticks",
    "take_profit_ticks",
    "trailing_stop_ticks",
    "breakeven_ticks",
    "atr_ratio_min",
    "max_hold_minutes",
    "entry_margin",
    "signal_mode",
    # текущие (до оптимизации) для сравнения
    "cur_profit_factor",
    "cur_total_pnl",
    "cur_n_trades",
    "cur_win_rate",
]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="*", help="Тикеры (по умолчанию — все из конфига)")
    parser.add_argument("--days", type=int, default=30, help="Глубина истории в днях (default: 30)")
    parser.add_argument("--min-trades", type=int, default=40, help="Мин. сделок (default: 40)")
    parser.add_argument("--out", default="result.csv", help="Имя выходного файла (default: result.csv)")
    args = parser.parse_args()

    rsi_cfg = load_yaml(RSI_CONFIG_PATH)
    instr_cfg = load_yaml(INSTRUMENTS_CONFIG_PATH)

    tickers = args.tickers if args.tickers else sorted(instr_cfg.keys())
    out_path = Path(args.out)

    print(f"Тикеров: {len(tickers)}  |  days={args.days}  min_trades={args.min_trades}")
    print(f"Результат → {out_path}\n")

    rows: list[dict] = []

    for i, ticker in enumerate(tickers, 1):
        if ticker not in rsi_cfg or ticker not in instr_cfg:
            print(f"[{i}/{len(tickers)}] {ticker}: нет в конфиге, пропуск")
            continue

        rsi_params = rsi_cfg[ticker]
        instr = instr_cfg[ticker]
        instr_params = {
            "ticker": ticker,
            "lot_size": instr.get("lot_size", 1),
            "tick_size": instr.get("tick_size", 0.01),
            "commission_rate": instr.get("commission_rate", 0.0004),
        }
        warmup = rsi_params.get("warmup_candles", 300)

        print(f"[{i}/{len(tickers)}] {ticker} — загружаю свечи...", end=" ", flush=True)
        try:
            candles = load_candles(figi=instr["figi"], ticker=ticker, days=args.days)
        except Exception as e:
            print(f"ОШИБКА загрузки свечей: {e}")
            continue

        print(f"{len(candles)} свечей, запускаю grid search...", end=" ", flush=True)

        try:
            cur_bt = run_backtest(
                candles=candles,
                rsi_params=rsi_params,
                instrument_params=instr_params,
                warmup_candles=warmup,
                days=args.days,
            )
            cur_m = cur_bt["metrics"]

            combo_count = [0]

            def _progress(done: int, total: int) -> None:
                combo_count[0] = done
                pct = int(done / total * 100) if total else 0
                print(f"\r[{i}/{len(tickers)}] {ticker} — {pct}%  ({done}/{total})", end="", flush=True)

            top_configs = optimize_ticker(
                candles=candles,
                rsi_params_base=rsi_params,
                instrument_params=instr_params,
                warmup_candles=warmup,
                progress_cb=_progress,
                min_trades=args.min_trades,
            )
            print()  # newline after progress

        except Exception as e:
            print(f"\nОШИБКА: {e}")
            continue

        if not top_configs:
            print(f"  → нет конфигов с min_trades>={args.min_trades}")
            continue

        print(f"  → топ-{len(top_configs)} конфигов, лучший PF={top_configs[0]['metrics']['profit_factor']:.2f}")

        for rank, cfg in enumerate(top_configs, 1):
            p = cfg["params"]
            m = cfg["metrics"]
            rows.append({
                "ticker": ticker,
                "rank": rank,
                "profit_factor": round(m.get("profit_factor", 0), 4),
                "total_pnl": round(m.get("total_pnl", 0), 2),
                "n_trades": m.get("n_trades", 0),
                "win_rate": round(m.get("win_rate", 0), 4),
                "ob_value": p.get("ob_value"),
                "os_value": p.get("os_value"),
                "stop_ticks": p.get("stop_ticks"),
                "take_profit_ticks": p.get("take_profit_ticks"),
                "trailing_stop_ticks": p.get("trailing_stop_ticks"),
                "breakeven_ticks": p.get("breakeven_ticks"),
                "atr_ratio_min": p.get("atr_ratio_min"),
                "max_hold_minutes": p.get("max_hold_minutes"),
                "entry_margin": p.get("entry_margin"),
                "signal_mode": p.get("signal_mode"),
                "cur_profit_factor": round(cur_m.get("profit_factor", 0), 4),
                "cur_total_pnl": round(cur_m.get("total_pnl", 0), 2),
                "cur_n_trades": cur_m.get("n_trades", 0),
                "cur_win_rate": round(cur_m.get("win_rate", 0), 4),
            })

    if not rows:
        print("Нет результатов.")
        return

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nГотово: {len(rows)} строк → {out_path}")


if __name__ == "__main__":
    main()
