"""
Маршруты главного дашборда.
"""
import json
from datetime import date

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from trading_bot.db import repository

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    today_pnl = repository.get_today_pnl()

    # Сделки за сегодня
    from trading_bot.db.repository import get_session
    from trading_bot.db.models import Trade
    from sqlalchemy import func
    with get_session() as session:
        today = date.today()
        today_trades = session.query(func.count(Trade.id)).filter(
            func.date(Trade.close_at) == today
        ).scalar() or 0
        total_trades = session.query(func.count(Trade.id)).scalar() or 0

        wins_today = session.query(func.count(Trade.id)).filter(
            func.date(Trade.close_at) == today,
            Trade.pnl_rub > 0,
        ).scalar() or 0

    win_rate = round(wins_today / today_trades * 100, 1) if today_trades > 0 else 0

    recent_signals = repository.get_recent_signals(limit=5)
    recent_trades, _ = repository.get_trades_page(page=1, per_page=5)
    pnl_by_day = repository.get_pnl_by_day(days=30)

    # Накопленный P&L (кумулятивная сумма)
    cumulative = []
    total = 0.0
    for d in pnl_by_day:
        total += d["pnl"]
        cumulative.append({"day": d["day"], "pnl": round(total, 2)})

    return render_template(
        "dashboard.html",
        today_pnl=today_pnl,
        today_trades=today_trades,
        total_trades=total_trades,
        win_rate=win_rate,
        recent_signals=recent_signals,
        recent_trades=recent_trades,
        chart_data=json.dumps(cumulative),
    )


@bp.route("/api/bot/toggle", methods=["POST"])
@login_required
def toggle_bot():
    current = repository.get_bot_active()
    repository.set_bot_active(not current)
    new_state = repository.get_bot_active()
    return jsonify({"active": new_state})


@bp.route("/api/bot/status")
@login_required
def bot_status():
    active = repository.get_bot_active()
    return jsonify({"active": active})


@bp.route("/api/position")
@login_required
def position():
    """Получить сводки по открытым позициям всех тикеров."""
    from trading_bot.web.app import get_position_managers
    pms = get_position_managers()
    positions = {
        ticker: pm.get_position_summary()
        for ticker, pm in pms.items()
    }
    return jsonify({"positions": positions})


@bp.route("/api/position/<ticker>/close", methods=["POST"])
@login_required
def close_position(ticker: str):
    """Принудительно закрыть открытую позицию по тикеру (по рынку)."""
    from trading_bot.web.app import get_position_managers
    from trading_bot.core.strategy.base_strategy import Signal, SignalType, SignalReason

    pms = get_position_managers()
    ticker = ticker.upper()
    if ticker not in pms:
        return jsonify({"ok": False, "error": "Тикер не найден"}), 404

    pm = pms[ticker]
    if not pm.has_position:
        return jsonify({"ok": False, "error": "Нет открытой позиции"}), 400

    manual_signal = Signal(
        signal_type=SignalType.EXIT,
        reason=SignalReason.MANUAL,
    )
    db_signal = pm._save_signal(
        signal_type="exit",
        ofi_value=pm._get_current_ofi(),
        reason="manual",
        acted_on=True,
    )
    pm._close_position(manual_signal, db_signal.id, exit_reason="manual")
    return jsonify({"ok": True})


@bp.route("/api/account")
@login_required
def account():
    """Получить баланс счёта и статистику портфеля."""
    from trading_bot.web.app import get_portfolio_manager
    pm = get_portfolio_manager()
    if pm is None:
        return jsonify({"portfolio_value": None, "open_positions": 0, "max_positions": 3})
    return jsonify(pm.get_summary())
