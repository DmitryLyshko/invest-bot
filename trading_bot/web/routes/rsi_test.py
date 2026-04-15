"""
RSI-тест: страница для визуальной проверки Augmented RSI по историческим свечам.
Загружает 5-мин свечи из T-Invest API, вычисляет ARSI/сигнальную линию и
возвращает их для отображения на графике.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from trading_bot.config import settings
from trading_bot.core.strategy.rsi_strategy import AugmentedRSI

logger = logging.getLogger(__name__)

bp = Blueprint("rsi_test", __name__)

MSK_OFFSET_HOURS = 3


def _to_msk_str(ts_utc) -> str:
    msk = ts_utc + timedelta(hours=MSK_OFFSET_HOURS)
    return msk.strftime("%d.%m %H:%M")


def _is_trading_hours_utc(ts_utc, rsi_params: dict) -> bool:
    trading_hours = rsi_params.get("trading_hours", {})
    start_str = trading_hours.get("start", "10:05")
    end_str = trading_hours.get("end", "18:30")
    skip_first = rsi_params.get("skip_first_minutes", 5)
    msk = ts_utc + timedelta(hours=MSK_OFFSET_HOURS)
    current_min = msk.hour * 60 + msk.minute
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    start_min = sh * 60 + sm + skip_first
    end_min = eh * 60 + em
    return start_min <= current_min < end_min

RSI_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH.parent / "rsi_config.yaml"
INSTRUMENTS_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH


def _load_rsi_config() -> dict:
    if not RSI_CONFIG_PATH.exists():
        return {}
    with open(RSI_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_instruments_config() -> dict:
    with open(INSTRUMENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@bp.route("/rsi-test")
@login_required
def index():
    rsi_config = _load_rsi_config()
    tickers = sorted(rsi_config.keys())
    selected = request.args.get("ticker", "").upper()
    if selected not in rsi_config:
        selected = tickers[0] if tickers else ""
    return render_template("rsi_test.html", tickers=tickers, selected=selected)


@bp.route("/api/rsi-test/<ticker>")
@login_required
def api_rsi_data(ticker: str):
    ticker = ticker.upper()
    rsi_config = _load_rsi_config()
    instruments = _load_instruments_config()

    if ticker not in rsi_config:
        return jsonify({"error": f"Тикер {ticker} не найден в rsi_config.yaml"}), 404
    if ticker not in instruments:
        return jsonify({"error": f"Тикер {ticker} не найден в instruments.yaml"}), 404

    rsi_params = rsi_config[ticker]
    figi = instruments[ticker]["figi"]

    # Загружаем исторические свечи из T-Invest API
    # Лимит API для 5-мин свечей: максимум 1 день за запрос → пагинируем
    days = int(request.args.get("days", 10))
    days = max(1, min(days, 30))

    now = datetime.now(timezone.utc)

    try:
        from tinkoff.invest import CandleInterval, Client
        from tinkoff.invest.sandbox.client import SandboxClient
        from tinkoff.invest.utils import quotation_to_decimal

        ClientClass = SandboxClient if settings.USE_SANDBOX else Client
        candles = []
        with ClientClass(settings.TINKOFF_TOKEN) as client:
            # Запрашиваем по 1 дню (ограничение API для 5-мин интервала)
            for day_offset in range(days - 1, -1, -1):
                chunk_to = now - timedelta(days=day_offset)
                chunk_from = chunk_to - timedelta(days=1)
                try:
                    resp = client.market_data.get_candles(
                        figi=figi,
                        from_=chunk_from,
                        to=chunk_to,
                        interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
                    )
                    candles.extend(c for c in resp.candles if c.is_complete)
                except Exception:
                    logger.debug(f"Пропуск чанка {chunk_from:%Y-%m-%d} для {ticker}")
                    continue
        # Сортируем и дедуплицируем по времени
        seen = set()
        unique = []
        for c in sorted(candles, key=lambda x: x.time):
            if c.time not in seen:
                seen.add(c.time)
                unique.append(c)
        candles = unique
    except Exception as e:
        logger.exception(f"Ошибка при загрузке свечей для {ticker}")
        return jsonify({"error": str(e)}), 500

    if not candles:
        return jsonify({"error": "Нет данных свечей за указанный период"}), 404

    # Прогрев RSI на первых warmup_candles свечах, затем собираем данные для графика
    from tinkoff.invest.utils import quotation_to_decimal

    length = rsi_params.get("length", 14)
    smooth = rsi_params.get("smooth", 14)
    smo_type_rsi = rsi_params.get("smo_type_rsi", "RMA")
    smo_type_signal = rsi_params.get("smo_type_signal", "EMA")
    ob = rsi_params.get("ob_value", 80.0)
    os_ = rsi_params.get("os_value", 20.0)

    # Прогрев: первые warmup_candles свечей только для инициализации RSI
    warmup_candles_count = rsi_params.get("warmup_candles", 300)
    # Берём последние N свечей для отображения на графике
    display_count = int(request.args.get("display", 200))
    display_count = max(50, min(display_count, 500))

    rsi = AugmentedRSI(
        length=length,
        smooth=smooth,
        smo_type_rsi=smo_type_rsi,
        smo_type_signal=smo_type_signal,
    )

    all_closes = [float(quotation_to_decimal(c.close)) for c in candles]

    # Разбиваем: прогрев + отображение
    if len(all_closes) > display_count:
        warmup_part = all_closes[:-display_count]
        display_part_candles = candles[-display_count:]
        display_part_closes = all_closes[-display_count:]
    else:
        warmup_part = []
        display_part_candles = candles
        display_part_closes = all_closes

    # Прогрев
    for close in warmup_part:
        rsi.update(close)

    # Параметры фильтрации (те же что в RSIStrategy)
    cooldown_sec = rsi_params.get("cooldown_seconds", 300)
    post_close_cd_sec = rsi_params.get("post_close_cooldown_seconds", 600)
    entry_margin = rsi_params.get("entry_margin", 10.0)
    use_crossover_exit = rsi_params.get("use_crossover_exit", False)

    # Симуляция состояния позиции для применения фильтров
    sim_pos = None               # None | 'long' | 'short'
    sim_last_entry_time = None
    sim_last_close_time = None
    sim_entry_signal_idx = None  # индекс в списке signals

    # Собираем данные для графика
    prev_arsi = None
    prev_signal = None
    timestamps = []
    arsi_values = []
    signal_values = []
    # signals: {idx, type, arsi, signal_line, prev_arsi, ts_utc, ts_msk,
    #           passed, blocked_reason, entry_signal_idx, exit_signal_idx}
    signals = []

    for i, (candle, close) in enumerate(zip(display_part_candles, display_part_closes)):
        result = rsi.update(close)
        ts_utc = candle.time
        ts_msk = _to_msk_str(ts_utc)

        if result is None:
            timestamps.append(ts_utc.isoformat())
            arsi_values.append(None)
            signal_values.append(None)
            prev_arsi = None
            prev_signal = None
            continue

        arsi, signal_line = result
        timestamps.append(ts_utc.isoformat())
        arsi_values.append(round(arsi, 2))
        signal_values.append(round(signal_line, 2))

        if prev_arsi is not None:
            # 1. Проверяем выход (crossover exit)
            if sim_pos is not None and use_crossover_exit and prev_signal is not None:
                exit_crossed = False
                exit_type = None
                if sim_pos == "long" and prev_arsi >= prev_signal and arsi < signal_line:
                    exit_crossed, exit_type = True, "exit_long"
                elif sim_pos == "short" and prev_arsi <= prev_signal and arsi > signal_line:
                    exit_crossed, exit_type = True, "exit_short"

                if exit_crossed:
                    exit_idx = len(signals)
                    signals.append({
                        "idx": i, "type": exit_type,
                        "arsi": round(arsi, 2), "signal_line": round(signal_line, 2),
                        "prev_arsi": round(prev_arsi, 2),
                        "ts_utc": ts_utc.isoformat(), "ts_msk": ts_msk,
                        "passed": True, "blocked_reason": None,
                        "entry_signal_idx": sim_entry_signal_idx, "exit_signal_idx": None,
                    })
                    if sim_entry_signal_idx is not None:
                        signals[sim_entry_signal_idx]["exit_signal_idx"] = exit_idx
                    sim_pos = None
                    sim_last_close_time = ts_utc
                    sim_entry_signal_idx = None

            # 2. Проверяем вход (если нет позиции)
            if sim_pos is None:
                sig_type = None
                if prev_arsi < os_ and arsi >= os_:
                    sig_type = "long"
                elif prev_arsi > ob and arsi <= ob:
                    sig_type = "short"

                if sig_type is not None:
                    in_hours = _is_trading_hours_utc(ts_utc, rsi_params)
                    blocked_reason = None

                    if not in_hours:
                        blocked_reason = "вне торговых часов"
                    elif (sim_last_entry_time is not None and
                          (ts_utc - sim_last_entry_time).total_seconds() < cooldown_sec):
                        rem = int(cooldown_sec - (ts_utc - sim_last_entry_time).total_seconds())
                        blocked_reason = f"кулдаун ({rem}с)"
                    elif (sim_last_close_time is not None and
                          (ts_utc - sim_last_close_time).total_seconds() < post_close_cd_sec):
                        rem = int(post_close_cd_sec - (ts_utc - sim_last_close_time).total_seconds())
                        blocked_reason = f"пост-закрытие ({rem}с)"
                    elif sig_type == "long" and arsi > os_ + entry_margin:
                        blocked_reason = f"margin (arsi {arsi:.1f} > {os_ + entry_margin:.0f})"
                    elif sig_type == "short" and arsi < ob - entry_margin:
                        blocked_reason = f"margin (arsi {arsi:.1f} < {ob - entry_margin:.0f})"

                    passed = blocked_reason is None
                    entry_idx = len(signals)
                    signals.append({
                        "idx": i, "type": sig_type,
                        "arsi": round(arsi, 2), "signal_line": round(signal_line, 2),
                        "prev_arsi": round(prev_arsi, 2),
                        "ts_utc": ts_utc.isoformat(), "ts_msk": ts_msk,
                        "passed": passed, "blocked_reason": blocked_reason,
                        "entry_signal_idx": None, "exit_signal_idx": None,
                    })
                    if passed:
                        sim_pos = sig_type
                        sim_last_entry_time = ts_utc
                        sim_entry_signal_idx = entry_idx

        prev_arsi = arsi
        prev_signal = signal_line

    # ATR-фильтр: последнее значение
    atr_info = None
    atr_ratio_min = rsi_params.get("atr_ratio_min", 0.0)
    if atr_ratio_min > 0 and len(candles) >= 5:
        from tinkoff.invest.utils import quotation_to_decimal as q2d
        atr_short_len = rsi_params.get("atr_length_short", 5)
        trs = [
            float(q2d(c.high)) - float(q2d(c.low))
            for c in candles
        ]
        short_atr = sum(trs[-atr_short_len:]) / atr_short_len
        long_atr = sum(trs) / len(trs)
        ratio = short_atr / long_atr if long_atr > 0 else 0
        atr_info = {
            "short_atr": round(short_atr, 4),
            "long_atr": round(long_atr, 4),
            "ratio": round(ratio, 3),
            "ratio_min": atr_ratio_min,
            "ok": ratio >= atr_ratio_min,
        }

    return jsonify({
        "ticker": ticker,
        "candles_total": len(candles),
        "display_count": len(display_part_candles),
        "ob": ob,
        "os": os_,
        "length": length,
        "smooth": smooth,
        "timestamps": timestamps,
        "arsi": arsi_values,
        "signal_line": signal_values,
        "signals": signals,
        "atr": atr_info,
        "params": {
            "cooldown_seconds": rsi_params.get("cooldown_seconds"),
            "post_close_cooldown_seconds": rsi_params.get("post_close_cooldown_seconds"),
            "trading_hours": rsi_params.get("trading_hours"),
            "skip_first_minutes": rsi_params.get("skip_first_minutes"),
            "atr_ratio_min": rsi_params.get("atr_ratio_min", 0),
            "entry_margin": rsi_params.get("entry_margin", 10.0),
            "use_crossover_exit": use_crossover_exit,
        },
    })


@bp.route("/api/rsi-test/<ticker>/diag")
@login_required
def api_rsi_diag(ticker: str):
    """Диагностика: сигналы и сделки из БД + последние bot_logs для RSI-стратегии."""
    ticker = ticker.upper()

    from sqlalchemy import func, or_
    from trading_bot.db.repository import get_session
    from trading_bot.db.models import Signal, BotLog, Instrument, Trade

    with get_session() as session:
        inst = session.query(Instrument).filter_by(ticker=ticker).first()
        if inst is None:
            return jsonify({"error": f"Инструмент {ticker} не найден в БД"}), 404

        instrument_id = inst.id
        rsi_filter = Signal.strategy_name == "rsi"

        # Последние 50 RSI-сигналов
        raw_signals = (
            session.query(Signal)
            .filter(Signal.instrument_id == instrument_id, rsi_filter)
            .order_by(Signal.created_at.desc())
            .limit(50)
            .all()
        )
        signals_data = [
            {
                "id": s.id,
                "type": s.signal_type,
                "reason": s.reason,
                "acted_on": s.acted_on,
                "ofi_value": round(s.ofi_value, 2) if s.ofi_value is not None else None,
                "created_at": s.created_at.isoformat(),
                "created_at_msk": _to_msk_str(s.created_at),
            }
            for s in raw_signals
        ]

        # Статистика сигналов
        reason_counts = (
            session.query(Signal.reason, func.count(Signal.id).label("cnt"))
            .filter(Signal.instrument_id == instrument_id, rsi_filter, Signal.acted_on == False)
            .group_by(Signal.reason)
            .all()
        )
        blocked_by_reason = {r.reason: r.cnt for r in reason_counts}

        total_rsi_signals = (
            session.query(func.count(Signal.id))
            .filter(Signal.instrument_id == instrument_id, rsi_filter)
            .scalar() or 0
        )
        acted_count = (
            session.query(func.count(Signal.id))
            .filter(Signal.instrument_id == instrument_id, rsi_filter, Signal.acted_on == True)
            .scalar() or 0
        )

        # Диагностика: сигналы с NULL strategy_name (были ли записаны до добавления колонки?)
        null_strategy_signals = (
            session.query(func.count(Signal.id))
            .filter(Signal.instrument_id == instrument_id, Signal.strategy_name.is_(None))
            .scalar() or 0
        )
        # Последний RSI-сигнал (чтобы видеть когда бот последний раз торговал RSI)
        last_signal_ts = None
        if total_rsi_signals > 0:
            last_sig = (
                session.query(Signal.created_at)
                .filter(Signal.instrument_id == instrument_id, rsi_filter)
                .order_by(Signal.created_at.desc())
                .first()
            )
            if last_sig:
                last_signal_ts = _to_msk_str(last_sig[0])

        # Последние 30 RSI-сделок из trades
        raw_trades = (
            session.query(Trade)
            .filter(Trade.instrument_id == instrument_id, Trade.strategy_name == "rsi")
            .order_by(Trade.close_at.desc())
            .limit(30)
            .all()
        )
        trades_data = [
            {
                "id": t.id,
                "direction": t.direction,
                "open_price": t.open_price,
                "close_price": t.close_price,
                "quantity": t.quantity,
                "pnl_rub": round(t.pnl_rub, 2),
                "exit_reason": t.exit_reason,
                "open_at": t.open_at.isoformat(),
                "close_at": t.close_at.isoformat(),
                "open_at_msk": _to_msk_str(t.open_at),
                "close_at_msk": _to_msk_str(t.close_at),
                "hold_seconds": t.hold_seconds,
            }
            for t in raw_trades
        ]
        total_trades = (
            session.query(func.count(Trade.id))
            .filter(Trade.instrument_id == instrument_id, Trade.strategy_name == "rsi")
            .scalar() or 0
        )
        total_pnl = (
            session.query(func.sum(Trade.pnl_rub))
            .filter(Trade.instrument_id == instrument_id, Trade.strategy_name == "rsi")
            .scalar() or 0.0
        )

        # Последние 30 bot_logs по тикеру / RSI / risk_manager
        logs = (
            session.query(BotLog)
            .filter(
                or_(
                    BotLog.message.like(f"%{ticker}%"),
                    BotLog.message.like("%rsi%"),
                    BotLog.component == "risk_manager",
                )
            )
            .order_by(BotLog.created_at.desc())
            .limit(30)
            .all()
        )
        logs_data = [
            {
                "level": l.level,
                "component": l.component,
                "message": l.message,
                "created_at": l.created_at.isoformat(),
                "created_at_msk": _to_msk_str(l.created_at),
            }
            for l in logs
        ]

    return jsonify({
        "ticker": ticker,
        "instrument_id": instrument_id,
        "total_rsi_signals": total_rsi_signals,
        "acted_count": acted_count,
        "blocked_by_reason": blocked_by_reason,
        "null_strategy_signals": null_strategy_signals,
        "last_signal_ts": last_signal_ts,
        "recent_signals": signals_data,
        "total_trades": total_trades,
        "total_pnl": round(float(total_pnl), 2),
        "recent_trades": trades_data,
        "recent_logs": logs_data,
    })
