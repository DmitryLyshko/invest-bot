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
    stats = repository.get_stats_summary()
    pnl_by_hour = repository.get_pnl_by_hour()
    pnl_by_weekday = repository.get_pnl_by_weekday()
    pnl_by_day = repository.get_pnl_by_day(days=90)

    return render_template(
        "stats.html",
        stats=stats,
        pnl_by_hour=json.dumps(pnl_by_hour),
        pnl_by_weekday=json.dumps(pnl_by_weekday),
        pnl_by_day=json.dumps(pnl_by_day),
    )
