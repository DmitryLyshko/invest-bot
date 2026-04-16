"""
Страница оптимизации параметров RSI-стратегии.

Grid search по пространству параметров → top-N конфигов на тикер.
Конфиг НЕ применяется автоматически — только после явного клика «Применить».
"""
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import yaml
from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from trading_bot.config import settings

logger = logging.getLogger(__name__)

bp = Blueprint("optimize", __name__)

RSI_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH.parent / "rsi_config.yaml"
INSTRUMENTS_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH

# ── In-memory state ───────────────────────────────────────────────────────────
_jobs: Dict[str, Dict[str, Any]] = {}
_results: Dict[str, Dict[str, Any]] = {}
_results_lock = threading.Lock()


def _load_rsi_config() -> dict:
    if not RSI_CONFIG_PATH.exists():
        return {}
    with open(RSI_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_instruments_config() -> dict:
    with open(INSTRUMENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _run_job(job_id: str, tickers: list, days: int, min_trades: int) -> None:
    """Фоновый поток: загружает свечи, считает базовые метрики, запускает grid search."""
    from trading_bot.backtest.candle_loader import load_candles
    from trading_bot.backtest.engine import run_backtest
    from trading_bot.backtest.optimizer import DEFAULT_GRID, optimize_ticker, total_combos

    rsi_cfg = _load_rsi_config()
    instr_cfg = _load_instruments_config()
    n_combos = total_combos(DEFAULT_GRID)

    total = len(tickers)
    _jobs[job_id]["total"] = total

    for idx, ticker in enumerate(tickers):
        _jobs[job_id].update(
            current_ticker=ticker,
            progress=idx,
            combo_progress=0,
            combo_total=n_combos,
        )

        if ticker not in rsi_cfg or ticker not in instr_cfg:
            logger.warning("[optimize] %s не найден в конфиге, пропуск", ticker)
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

        def _progress(done: int, tot: int, _jid: str = job_id) -> None:
            _jobs[_jid]["combo_progress"] = done
            _jobs[_jid]["combo_total"] = tot

        try:
            candles = load_candles(figi=instr["figi"], ticker=ticker, days=days)

            # Базовые метрики с текущими параметрами (для сравнения)
            current_bt = run_backtest(
                candles=candles,
                rsi_params=rsi_params,
                instrument_params=instr_params,
                warmup_candles=warmup,
                days=days,
            )

            # Grid search
            top_configs = optimize_ticker(
                candles=candles,
                rsi_params_base=rsi_params,
                instrument_params=instr_params,
                warmup_candles=warmup,
                progress_cb=_progress,
                min_trades=min_trades,
            )

            with _results_lock:
                _results[ticker] = {
                    "ticker": ticker,
                    "days": days,
                    "current_metrics": current_bt["metrics"],
                    "current_params": {
                        "ob_value":            rsi_params.get("ob_value", 80.0),
                        "os_value":            rsi_params.get("os_value", 20.0),
                        "stop_ticks":          rsi_params.get("stop_ticks", 80),
                        "take_profit_ticks":   rsi_params.get("take_profit_ticks", 0),
                        "trailing_stop_ticks": rsi_params.get("trailing_stop_ticks", 0),
                        "breakeven_ticks":     rsi_params.get("breakeven_ticks", 0),
                        "atr_ratio_min":       rsi_params.get("atr_ratio_min", 0.0),
                    },
                    "top_configs": top_configs,
                    "run_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M"),
                }

        except Exception as exc:
            logger.exception("[optimize] Ошибка для %s: %s", ticker, exc)
            with _results_lock:
                _results[ticker] = {
                    "ticker": ticker,
                    "error": str(exc),
                    "run_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M"),
                }

    _jobs[job_id].update(progress=total, status="done", current_ticker=None)
    logger.info("[optimize] Job %s завершён: %d тикеров", job_id, total)


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/optimize")
@login_required
def index():
    rsi_cfg = _load_rsi_config()
    tickers = sorted(rsi_cfg.keys())
    has_results = bool(_results)
    return render_template("optimize.html", tickers=tickers, has_results=has_results)


@bp.route("/api/optimize/run", methods=["POST"])
@login_required
def run():
    data = request.get_json(force=True, silent=True) or {}
    days = max(7, min(int(data.get("days", 60)), 180))
    min_trades = max(5, int(data.get("min_trades", 10)))

    rsi_cfg = _load_rsi_config()
    all_tickers = sorted(rsi_cfg.keys())

    selected = data.get("tickers", "all")
    if selected == "all":
        tickers = all_tickers
    elif isinstance(selected, list):
        tickers = [t for t in selected if t in rsi_cfg]
    else:
        tickers = [selected] if selected in rsi_cfg else all_tickers

    if not tickers:
        return jsonify({"error": "Нет тикеров для оптимизации"}), 400

    from trading_bot.backtest.optimizer import total_combos
    n_combos = total_combos()

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "total": len(tickers),
        "current_ticker": None,
        "combo_progress": 0,
        "combo_total": n_combos,
        "error": None,
    }

    threading.Thread(
        target=_run_job,
        args=(job_id, tickers, days, min_trades),
        daemon=True,
        name=f"optimize_{job_id}",
    ).start()

    return jsonify({"job_id": job_id, "total": len(tickers), "combos_per_ticker": n_combos})


@bp.route("/api/optimize/status/<job_id>")
@login_required
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job не найден"}), 404
    return jsonify(job)


@bp.route("/api/optimize/results")
@login_required
def results():
    """Сводная таблица: текущий PF vs лучший найденный PF."""
    with _results_lock:
        summary = []
        for ticker, res in sorted(_results.items()):
            if "error" in res:
                summary.append({
                    "ticker": ticker,
                    "error": res["error"],
                    "run_at": res.get("run_at", ""),
                })
                continue

            cm = res.get("current_metrics", {})
            top = res.get("top_configs", [])
            bm = top[0]["metrics"] if top else {}

            current_pf = cm.get("profit_factor", 0.0)
            best_pf = bm.get("profit_factor", 0.0)

            summary.append({
                "ticker":         ticker,
                "days":           res.get("days", 0),
                "current_pf":     round(current_pf, 2),
                "current_trades": cm.get("n_trades", 0),
                "current_winrate": cm.get("win_rate", 0.0),
                "best_pf":        round(best_pf, 2),
                "best_trades":    bm.get("n_trades", 0),
                "best_winrate":   bm.get("win_rate", 0.0),
                "configs_found":  len(top),
                "improved":       best_pf > current_pf,
                "run_at":         res.get("run_at", ""),
            })
    return jsonify(summary)


@bp.route("/api/optimize/results/<ticker>")
@login_required
def ticker_result(ticker: str):
    """Полный результат: текущие параметры + топ-10 найденных конфигов."""
    ticker = ticker.upper()
    with _results_lock:
        res = _results.get(ticker)
    if res is None:
        return jsonify({"error": f"Нет результатов для {ticker}"}), 404
    return jsonify(res)


@bp.route("/api/optimize/export.csv")
@login_required
def export_csv():
    """
    CSV со всеми найденными конфигами по всем тикерам.
    Структура: один ряд = один конфиг из top_configs.
    Текущий конфиг идёт отдельной строкой с rank=0 для сравнения.
    """
    import csv
    import io

    FIELDNAMES = [
        "ticker", "signal_mode", "days", "rank",
        "ob_value", "os_value",
        "stop_ticks", "take_profit_ticks", "trailing_stop_ticks", "breakeven_ticks",
        "atr_ratio_min", "max_hold_minutes",
        "n_trades", "win_rate", "total_pnl", "profit_factor",
        "max_drawdown", "avg_hold_candles",
        "exit_stop_loss", "exit_take_profit", "exit_trailing_stop",
        "exit_breakeven_stop", "exit_timeout", "exit_eod_close", "exit_other",
    ]

    def _exit_counts(exit_reasons: dict) -> dict:
        known = {"stop_loss", "take_profit", "trailing_stop",
                 "breakeven_stop", "timeout", "eod_close"}
        other = sum(v for k, v in exit_reasons.items() if k not in known)
        return {
            "exit_stop_loss":      exit_reasons.get("stop_loss", 0),
            "exit_take_profit":    exit_reasons.get("take_profit", 0),
            "exit_trailing_stop":  exit_reasons.get("trailing_stop", 0),
            "exit_breakeven_stop": exit_reasons.get("breakeven_stop", 0),
            "exit_timeout":        exit_reasons.get("timeout", 0),
            "exit_eod_close":      exit_reasons.get("eod_close", 0),
            "exit_other":          other,
        }

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES, lineterminator="\n")
    writer.writeheader()

    with _results_lock:
        snapshot = dict(_results)

    for ticker, res in sorted(snapshot.items()):
        if "error" in res:
            continue

        signal_mode = res.get("signal_mode", "mean_reversion")
        days = res.get("days", 0)
        cp = res.get("current_params", {})
        cm = res.get("current_metrics", {})

        # rank=0 — текущий конфиг (для сравнения)
        row = {"ticker": ticker, "signal_mode": signal_mode, "days": days, "rank": 0}
        row.update({k: cp.get(k, "") for k in
                    ("ob_value", "os_value", "stop_ticks", "take_profit_ticks",
                     "trailing_stop_ticks", "breakeven_ticks", "atr_ratio_min", "max_hold_minutes")})
        row.update({
            "n_trades":        cm.get("n_trades", 0),
            "win_rate":        cm.get("win_rate", 0.0),
            "total_pnl":       cm.get("total_pnl", 0.0),
            "profit_factor":   cm.get("profit_factor", 0.0),
            "max_drawdown":    cm.get("max_drawdown", 0.0),
            "avg_hold_candles": cm.get("avg_hold_candles", 0.0),
        })
        row.update(_exit_counts(cm.get("exit_reasons", {})))
        writer.writerow(row)

        # rank=1..N — найденные конфиги
        for rank, cfg in enumerate(res.get("top_configs", []), start=1):
            p = cfg["params"]
            m = cfg["metrics"]
            row = {"ticker": ticker, "signal_mode": p.get("signal_mode", signal_mode), "days": days, "rank": rank}
            row.update({k: p.get(k, "") for k in
                        ("ob_value", "os_value", "stop_ticks", "take_profit_ticks",
                         "trailing_stop_ticks", "breakeven_ticks", "atr_ratio_min", "max_hold_minutes")})
            row.update({
                "n_trades":        m.get("n_trades", 0),
                "win_rate":        m.get("win_rate", 0.0),
                "total_pnl":       m.get("total_pnl", 0.0),
                "profit_factor":   m.get("profit_factor", 0.0),
                "max_drawdown":    m.get("max_drawdown", 0.0),
                "avg_hold_candles": m.get("avg_hold_candles", 0.0),
            })
            row.update(_exit_counts(m.get("exit_reasons", {})))
            writer.writerow(row)

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"optimize_{ts}.csv"

    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.route("/api/optimize/apply/<ticker>", methods=["POST"])
@login_required
def apply_config(ticker: str):
    """
    Применить выбранный конфиг из top_configs к rsi_config.yaml.
    Обновляются только оптимизированные параметры; остальные (trading_hours,
    cooldowns, lot_size и пр.) остаются без изменений.
    """
    ticker = ticker.upper()
    data = request.get_json(force=True, silent=True) or {}
    config_idx = int(data.get("config_idx", 0))

    with _results_lock:
        res = _results.get(ticker)

    if res is None or "error" in res:
        return jsonify({"error": f"Нет результатов для {ticker}"}), 404

    top_configs = res.get("top_configs", [])
    if not top_configs or config_idx >= len(top_configs):
        return jsonify({"error": "Конфиг не найден"}), 404

    new_params = top_configs[config_idx]["params"]

    try:
        with open(RSI_CONFIG_PATH, "r", encoding="utf-8") as f:
            rsi_cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        return jsonify({"error": f"Ошибка чтения конфига: {exc}"}), 500

    if ticker not in rsi_cfg:
        return jsonify({"error": f"Тикер {ticker} не найден в rsi_config.yaml"}), 404

    # Обновляем только те поля, которые оптимизировались
    OPTIMIZED_KEYS = [
        "ob_value", "os_value",
        "stop_ticks", "take_profit_ticks", "trailing_stop_ticks", "breakeven_ticks",
        "atr_ratio_min", "max_hold_minutes", "entry_margin", "signal_mode",
    ]
    for key in OPTIMIZED_KEYS:
        if key in new_params:
            rsi_cfg[ticker][key] = new_params[key]

    try:
        with open(RSI_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(rsi_cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        return jsonify({"error": f"Ошибка записи конфига: {exc}"}), 500

    logger.info("[optimize] Применён конфиг #%d для %s: %s", config_idx, ticker, new_params)
    return jsonify({"ok": True, "ticker": ticker, "applied_params": new_params})
