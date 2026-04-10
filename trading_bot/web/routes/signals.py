"""
Маршруты журнала сигналов и статистики.
"""
import json

from flask import Blueprint, render_template, request
from flask_login import login_required

from trading_bot.db import repository

bp_signals = Blueprint("signals", __name__)
bp_stats = Blueprint("stats", __name__)


@bp_signals.route("/signals")
@login_required
def index():
    page = int(request.args.get("page", 1))
    per_page = 50

    signals, total = repository.get_signals_page(page=page, per_page=per_page)
    total_pages = (total + per_page - 1) // per_page

    return render_template(
        "signals.html",
        signals=signals,
        page=page,
        total_pages=total_pages,
        total=total,
    )


@bp_stats.route("/stats")
@login_required
def index():
    current_ticker = request.args.get("ticker", "").strip().upper()
    instrument_id = None
    if current_ticker:
        inst = repository.get_instrument_by_ticker(current_ticker)
        if inst:
            instrument_id = inst.id
        else:
            current_ticker = ""

    instruments = repository.get_active_instruments()
    stats = repository.get_stats_summary(instrument_id=instrument_id)
    pnl_by_hour = repository.get_pnl_by_hour(instrument_id=instrument_id)
    pnl_by_weekday = repository.get_pnl_by_weekday(instrument_id=instrument_id)
    pnl_by_day = repository.get_pnl_by_day(days=90)

    return render_template(
        "stats.html",
        stats=stats,
        pnl_by_hour=json.dumps(pnl_by_hour),
        pnl_by_weekday=json.dumps(pnl_by_weekday),
        pnl_by_day=json.dumps(pnl_by_day),
        instruments=instruments,
        current_ticker=current_ticker,
    )
