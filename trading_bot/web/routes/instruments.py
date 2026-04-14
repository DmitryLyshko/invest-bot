"""
Маршруты управления конфигурацией инструментов (instruments.yaml).
"""
import yaml
from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_required

from trading_bot.config import settings
from trading_bot.db import repository

bp = Blueprint("instruments", __name__)


def _load_yaml() -> dict:
    with open(settings.INSTRUMENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml(config: dict) -> None:
    with open(settings.INSTRUMENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _form_to_params(form) -> dict:
    return {
        "figi": form.get("figi", "").strip(),
        "instrument_id": form.get("instrument_id", "").strip(),
        "lot_size": int(form.get("lot_size", 1)),
        "commission_rate": float(form.get("commission_rate", 0.0004)),
        "ofi_threshold": float(form.get("ofi_threshold", 0.7)),
        "ofi_levels": int(form.get("ofi_levels", 5)),
        "ofi_smooth_window": int(form.get("ofi_smooth_window", 12)),
        "ofi_exit_threshold": float(form.get("ofi_exit_threshold", 0.5)),
        "min_ofi_confirmations": int(form.get("min_ofi_confirmations", 4)),
        "min_profit_ticks_for_ofi_exit": int(form.get("min_profit_ticks_for_ofi_exit", 0)),
        "print_multiplier": float(form.get("print_multiplier", 8.0)),
        "print_window": int(form.get("print_window", 200)),
        "tick_size": float(form.get("tick_size", 0.01)),
        "stop_ticks": int(form.get("stop_ticks", 30)),
        "breakeven_ticks": int(form.get("breakeven_ticks", 25)),
        "take_profit_ticks": int(form.get("take_profit_ticks", 0)),
        "max_position_lots": int(form.get("max_position_lots", 1)),
        "max_hold_minutes": int(form.get("max_hold_minutes", 60)),
        "min_hold_seconds": int(form.get("min_hold_seconds", 120)),
        "cooldown_seconds": int(form.get("cooldown_seconds", 60)),
        "post_close_cooldown_seconds": int(form.get("post_close_cooldown_seconds", 90)),
        "trading_hours": {
            "start": form.get("trading_hours_start", "10:05"),
            "end": form.get("trading_hours_end", "18:30"),
        },
        "skip_first_minutes": int(form.get("skip_first_minutes", 5)),
        "ofi_scale": float(form.get("ofi_scale", 1000.0)),
        "trend_ma_window": int(form.get("trend_ma_window", 1000)),
        "min_ofi_entry_confirmations": int(form.get("min_ofi_entry_confirmations", 1)),
        "trailing_stop_ticks": int(form.get("trailing_stop_ticks", 0)),
        "print_max_age_seconds": int(form.get("print_max_age_seconds", 15)),
        "ofi_auto_calibrate_window": int(form.get("ofi_auto_calibrate_window", 0)),
    }


def _upsert_db(ticker: str, params: dict) -> None:
    """Синхронизировать поля инструмента в БД (только те, что есть в модели)."""
    repository.upsert_instrument({
        "ticker": ticker,
        "figi": params["figi"],
        "lot_size": params["lot_size"],
        "ofi_threshold": params.get("ofi_threshold", 0.7),
        "print_multiplier": params.get("print_multiplier", 8.0),
        "print_window": params.get("print_window", 200),
        "ofi_levels": params.get("ofi_levels", 5),
        "cooldown_seconds": params.get("cooldown_seconds", 60),
        "max_hold_minutes": params.get("max_hold_minutes", 60),
        "stop_ticks": params.get("stop_ticks", 30),
        "ofi_smooth_window": params.get("ofi_smooth_window", 12),
        "min_hold_seconds": params.get("min_hold_seconds", 120),
        "ofi_exit_threshold": params.get("ofi_exit_threshold", 0.5),
        "min_ofi_confirmations": params.get("min_ofi_confirmations", 4),
        "is_active": True,
    })


@bp.route("/instruments")
@login_required
def index():
    yaml_config = _load_yaml()
    db_instruments = repository.get_active_instruments()
    ticker_to_id = {inst.ticker: inst.id for inst in db_instruments}

    ticker_stats = {}
    for ticker in yaml_config:
        inst_id = ticker_to_id.get(ticker)
        ticker_stats[ticker] = repository.get_stats_summary(instrument_id=inst_id) if inst_id else None

    return render_template("instruments.html", yaml_config=yaml_config, ticker_stats=ticker_stats)


@bp.route("/instruments/<ticker>/edit", methods=["GET", "POST"])
@login_required
def edit(ticker):
    yaml_config = _load_yaml()
    if ticker not in yaml_config:
        return redirect(url_for("instruments.index"))

    error = None
    if request.method == "POST":
        try:
            params = _form_to_params(request.form)
            yaml_config[ticker] = params
            _save_yaml(yaml_config)
            _upsert_db(ticker, params)
            return redirect(url_for("instruments.index"))
        except (ValueError, KeyError) as e:
            error = f"Ошибка в данных формы: {e}"

    return render_template("instrument_edit.html", ticker=ticker, params=yaml_config[ticker], error=error)


@bp.route("/instruments/add", methods=["POST"])
@login_required
def add():
    ticker = request.form.get("ticker", "").strip().upper()
    figi = request.form.get("figi", "").strip()
    if not ticker or not figi:
        return redirect(url_for("instruments.index"))

    yaml_config = _load_yaml()
    if ticker not in yaml_config:
        yaml_config[ticker] = {
            "figi": figi,
            "instrument_id": "",
            "lot_size": 1,
            "commission_rate": 0.0004,
            "ofi_threshold": 0.7,
            "ofi_levels": 5,
            "ofi_smooth_window": 12,
            "ofi_exit_threshold": 0.5,
            "min_ofi_confirmations": 4,
            "min_profit_ticks_for_ofi_exit": 0,
            "print_multiplier": 8.0,
            "print_window": 200,
            "tick_size": 0.01,
            "stop_ticks": 30,
            "breakeven_ticks": 25,
            "take_profit_ticks": 90,
            "max_position_lots": 1,
            "max_hold_minutes": 60,
            "min_hold_seconds": 120,
            "cooldown_seconds": 60,
            "post_close_cooldown_seconds": 90,
            "trading_hours": {"start": "10:05", "end": "18:30"},
            "skip_first_minutes": 5,
            "ofi_scale": 1000,
            "trend_ma_window": 1000,
            "min_ofi_entry_confirmations": 1,
            "trailing_stop_ticks": 0,
            "print_max_age_seconds": 15,
            "ofi_auto_calibrate_window": 0,
        }
        _save_yaml(yaml_config)
        _upsert_db(ticker, yaml_config[ticker])

    return redirect(url_for("instruments.edit", ticker=ticker))
