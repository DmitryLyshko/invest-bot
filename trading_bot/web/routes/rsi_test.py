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

    # Собираем данные для графика
    prev_arsi = None
    timestamps = []
    arsi_values = []
    signal_values = []
    signals = []  # {idx, type: 'long'|'short', arsi, ts}

    for i, (candle, close) in enumerate(zip(display_part_candles, display_part_closes)):
        result = rsi.update(close)
        if result is None:
            timestamps.append(candle.time.isoformat())
            arsi_values.append(None)
            signal_values.append(None)
            prev_arsi = None
            continue

        arsi, signal_line = result
        ts_str = candle.time.isoformat()
        timestamps.append(ts_str)
        arsi_values.append(round(arsi, 2))
        signal_values.append(round(signal_line, 2))

        # Детектируем пересечения (сигналы входа)
        if prev_arsi is not None:
            if prev_arsi < os_ and arsi >= os_:
                signals.append({"idx": i, "type": "long", "arsi": round(arsi, 2), "ts": ts_str})
            elif prev_arsi > ob and arsi <= ob:
                signals.append({"idx": i, "type": "short", "arsi": round(arsi, 2), "ts": ts_str})

        prev_arsi = arsi

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
        },
    })
