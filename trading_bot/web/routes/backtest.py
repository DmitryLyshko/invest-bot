"""
Страница бэктеста RSI-стратегии.

Запуск бэктеста — фоновый поток, прогресс отслеживается через polling.
Результаты кэшируются в памяти (пересчитываются при новом запуске).
"""
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import yaml
from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from trading_bot.config import settings

logger = logging.getLogger(__name__)

bp = Blueprint("backtest", __name__)

RSI_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH.parent / "rsi_config.yaml"
INSTRUMENTS_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH

# ── In-memory state ───────────────────────────────────────────────────────────
# job_id → {status, progress, total, current_ticker, error}
_jobs: Dict[str, Dict[str, Any]] = {}
# ticker → backtest result dict
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


def _run_job(job_id: str, tickers: list, days: int) -> None:
    """Фоновый поток: прогоняет бэктест по каждому тикеру последовательно."""
    from trading_bot.backtest.candle_loader import load_candles
    from trading_bot.backtest.engine import run_backtest

    rsi_cfg = _load_rsi_config()
    instr_cfg = _load_instruments_config()

    total = len(tickers)
    _jobs[job_id]["total"] = total

    for idx, ticker in enumerate(tickers):
        _jobs[job_id]["current_ticker"] = ticker
        _jobs[job_id]["progress"] = idx

        if ticker not in rsi_cfg or ticker not in instr_cfg:
            logger.warning(f"[backtest] {ticker} не найден в конфиге, пропуск")
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

        try:
            candles = load_candles(
                figi=instr["figi"],
                ticker=ticker,
                days=days,
            )
            result = run_backtest(
                candles=candles,
                rsi_params=rsi_params,
                instrument_params=instr_params,
                warmup_candles=warmup,
                days=days,
            )
            with _results_lock:
                _results[ticker] = result
        except Exception as e:
            logger.exception(f"[backtest] Ошибка для {ticker}: {e}")
            with _results_lock:
                _results[ticker] = {
                    "ticker": ticker,
                    "days": days,
                    "error": str(e),
                    "trades": [],
                    "equity_curve": [],
                    "metrics": {},
                    "run_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M МСК"),
                }

    _jobs[job_id]["progress"] = total
    _jobs[job_id]["status"] = "done"
    _jobs[job_id]["current_ticker"] = None
    logger.info(f"[backtest] Job {job_id} завершён: {total} тикеров")


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/backtest")
@login_required
def index():
    rsi_cfg = _load_rsi_config()
    tickers = sorted(rsi_cfg.keys())
    has_results = bool(_results)
    return render_template("backtest.html", tickers=tickers, has_results=has_results)


@bp.route("/api/backtest/run", methods=["POST"])
@login_required
def run():
    data = request.get_json(force=True, silent=True) or {}
    days = int(data.get("days", 60))
    days = max(7, min(days, 180))

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
        return jsonify({"error": "Нет тикеров для бэктеста"}), 400

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "total": len(tickers),
        "current_ticker": None,
        "error": None,
    }

    t = threading.Thread(
        target=_run_job,
        args=(job_id, tickers, days),
        daemon=True,
        name=f"backtest_{job_id}",
    )
    t.start()

    return jsonify({"job_id": job_id, "total": len(tickers)})


@bp.route("/api/backtest/status/<job_id>")
@login_required
def status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job не найден"}), 404
    return jsonify(job)


@bp.route("/api/backtest/results")
@login_required
def results():
    """Сводная таблица по всем тикерам с закэшированными результатами."""
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
            m = res.get("metrics", {})
            summary.append({
                "ticker": ticker,
                "days": res.get("days", 0),
                "candles_total": res.get("candles_total", 0),
                "candles_used": res.get("candles_used", 0),
                "n_trades": m.get("n_trades", 0),
                "win_rate": m.get("win_rate", 0.0),
                "total_pnl": m.get("total_pnl", 0.0),
                "profit_factor": m.get("profit_factor", 0.0),
                "max_drawdown": m.get("max_drawdown", 0.0),
                "avg_hold_candles": m.get("avg_hold_candles", 0.0),
                "run_at": res.get("run_at", ""),
            })
    return jsonify(summary)


@bp.route("/api/backtest/results/<ticker>")
@login_required
def ticker_result(ticker: str):
    """Полный результат для одного тикера (включая trades и equity_curve)."""
    ticker = ticker.upper()
    with _results_lock:
        res = _results.get(ticker)
    if res is None:
        return jsonify({"error": f"Нет результатов для {ticker}"}), 404
    return jsonify(res)
