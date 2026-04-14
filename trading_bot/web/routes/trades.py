"""
Маршруты журнала сделок.
"""
import csv
import io
from datetime import date

from flask import Blueprint, make_response, render_template, request
from flask_login import login_required

from trading_bot.db import repository

bp = Blueprint("trades", __name__)


@bp.route("/trades")
@login_required
def index():
    page = int(request.args.get("page", 1))
    per_page = 50
    direction = request.args.get("direction", "")
    exit_reason = request.args.get("exit_reason", "")
    date_from_str = request.args.get("date_from", "")
    date_to_str = request.args.get("date_to", "")
    strategy_filter = request.args.get("strategy", "")

    date_from = date.fromisoformat(date_from_str) if date_from_str else None
    date_to = date.fromisoformat(date_to_str) if date_to_str else None

    trades, total = repository.get_trades_page(
        page=page,
        per_page=per_page,
        direction=direction or None,
        exit_reason=exit_reason or None,
        date_from=date_from,
        date_to=date_to,
        strategy_name=strategy_filter or None,
    )

    total_pages = (total + per_page - 1) // per_page

    return render_template(
        "trades.html",
        trades=trades,
        page=page,
        total_pages=total_pages,
        total=total,
        direction=direction,
        exit_reason=exit_reason,
        date_from=date_from_str,
        date_to=date_to_str,
        strategy_filter=strategy_filter,
    )


@bp.route("/trades/export")
@login_required
def export_csv():
    direction = request.args.get("direction", "")
    exit_reason = request.args.get("exit_reason", "")
    date_from_str = request.args.get("date_from", "")
    date_to_str = request.args.get("date_to", "")
    strategy_filter = request.args.get("strategy", "")

    date_from = date.fromisoformat(date_from_str) if date_from_str else None
    date_to = date.fromisoformat(date_to_str) if date_to_str else None

    trades = repository.get_all_trades_for_export(
        direction=direction or None,
        exit_reason=exit_reason or None,
        date_from=date_from,
        date_to=date_to,
        strategy_name=strategy_filter or None,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Стратегия", "Дата открытия", "Дата закрытия", "Направление",
        "Цена входа", "Цена выхода", "Лотов", "P&L руб",
        "Комиссия руб", "Время удержания (сек)", "Причина выхода",
    ])
    for t in trades:
        writer.writerow([
            t.id,
            t.strategy_name or "combo",
            t.open_at.strftime("%Y-%m-%d %H:%M:%S") if t.open_at else "",
            t.close_at.strftime("%Y-%m-%d %H:%M:%S") if t.close_at else "",
            t.direction,
            t.open_price,
            t.close_price,
            t.quantity,
            t.pnl_rub,
            t.commission_rub,
            t.hold_seconds,
            t.exit_reason,
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=trades.csv"
    return response
