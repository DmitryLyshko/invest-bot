"""
Маршруты управления стратегиями (включить / выключить).
"""
from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from trading_bot.db import repository

bp = Blueprint("strategies", __name__)

STRATEGY_META = {
    "combo": {
        "display_name": "Combo (OFI + Large Print)",
        "description": (
            "Стратегия на стакане: вход по совпадению сигнала Order Flow Imbalance "
            "и крупного принта. Работает на тиковых данных в реальном времени."
        ),
        "timeframe": "тики",
        "doc": "OFI измеряет дисбаланс заявок на покупку/продажу. "
               "Крупный принт — сделка объёмом ≥ median × multiplier.",
    },
    "rsi": {
        "display_name": "Augmented RSI 5m (LuxAlgo)",
        "description": (
            "Стратегия на 5-минутных свечах: вход при выходе RSI из зоны "
            "перепроданности (< 20) или перекупленности (> 80)."
        ),
        "timeframe": "5 мин",
        "doc": "Использует модифицированный RSI (LuxAlgo): учитывает диапазон "
               "свечи (high−low) вместо просто закрытий.",
    },
}


@bp.route("/strategies")
@login_required
def index():
    states = {s.strategy_name: s for s in repository.get_all_strategy_states()}
    strategy_pnl = {
        row["strategy_name"]: row
        for row in repository.get_strategy_pnl_summary()
    }
    return render_template(
        "strategies.html",
        strategy_meta=STRATEGY_META,
        states=states,
        strategy_pnl=strategy_pnl,
    )


@bp.route("/api/strategies/<name>/toggle", methods=["POST"])
@login_required
def toggle(name: str):
    if name not in STRATEGY_META:
        return jsonify({"error": "Unknown strategy"}), 400
    current = repository.get_strategy_active(name)
    repository.set_strategy_active(name, not current)
    new_state = repository.get_strategy_active(name)
    repository.log_event(
        "INFO",
        "strategies",
        f"Стратегия '{name}' {'включена' if new_state else 'выключена'}",
    )
    return jsonify({"active": new_state})
