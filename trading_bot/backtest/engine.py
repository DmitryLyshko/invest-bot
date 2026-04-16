"""
Ядро бэктеста RSI-стратегии на OHLCV свечах из T-Invest API.

Логика входа/выхода максимально приближена к live RSIStrategy:
  - Crossover ARSI → pending entry (вход по open следующей свечи)
  - Стоп/тейк/трейлинг/безубыток по high/low каждой свечи
  - Пессимизм: если стоп и тейк в одной свече — стоп побеждает
  - ATR-фильтр: rolling short_atr / long_atr
  - Кулдауны и торговые часы
  - EOD-закрытие: принудительный выход в eod_close_time (как в live)
  - entry_margin: фильтр фантомных пересечений (аналог live-стратегии)

Режимы сигнала (signal_mode):
  "mean_reversion" (по умолчанию)
      LONG  — arsi пересекает os снизу вверх (выход из перепроданности)
      SHORT — arsi пересекает ob сверху вниз (выход из перекупленности)
  "trend"
      LONG  — arsi пересекает ob снизу вверх (подтверждение роста)
      SHORT — arsi пересекает os сверху вниз (подтверждение падения)

PnL считается на 1 лот (quantity_lots=1) для нормализованного сравнения.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from trading_bot.core.strategy.rsi_strategy import AugmentedRSI

logger = logging.getLogger(__name__)

MSK_OFFSET_HOURS = 3


def _msk_str(ts: datetime) -> str:
    msk = ts + timedelta(hours=MSK_OFFSET_HOURS)
    return msk.strftime("%d.%m %H:%M")


def _compute_metrics(trades: List[Dict]) -> Dict[str, Any]:
    if not trades:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_hold_candles": 0.0,
            "exit_reasons": {},
        }

    n = len(trades)
    wins = [t for t in trades if t["pnl_rub"] > 0]
    losses = [t for t in trades if t["pnl_rub"] <= 0]

    gross_profit = sum(t["pnl_rub"] for t in wins)
    gross_loss = abs(sum(t["pnl_rub"] for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0

    # Max drawdown (absolute ₽ from equity peak)
    peak = 0.0
    max_dd = 0.0
    cumulative = 0.0
    for t in trades:
        cumulative += t["pnl_rub"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    exit_reasons: Dict[str, int] = {}
    for t in trades:
        r = t["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        "n_trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "total_pnl": round(sum(t["pnl_rub"] for t in trades), 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_dd, 2),
        "avg_hold_candles": round(sum(t["hold_candles"] for t in trades) / n, 1),
        "exit_reasons": exit_reasons,
    }


def run_backtest(
    candles: List[Dict],
    rsi_params: Dict[str, Any],
    instrument_params: Dict[str, Any],
    warmup_candles: int = 300,
    days: int = 0,
) -> Dict[str, Any]:
    """
    Запустить бэктест по списку OHLCV свечей.

    Args:
        candles:          список от candle_loader.load_candles()
        rsi_params:       секция из rsi_config.yaml
        instrument_params: секция из instruments.yaml (lot_size, tick_size, ...)
        warmup_candles:   первые N свечей — только прогрев RMA, без торговли
        days:             передаётся в результат для информации

    Returns:
        dict: ticker, days, candles_total, candles_used, trades,
              equity_curve, metrics, signal_mode, run_at
    """
    ticker = instrument_params.get("ticker", "")
    lot_size = instrument_params.get("lot_size", 1)
    tick_size = float(instrument_params.get("tick_size", 0.01))
    commission_rate = float(instrument_params.get("commission_rate", 0.0004))

    # ── RSI параметры ─────────────────────────────────────────────────────────
    length = rsi_params.get("length", 10)
    smooth = rsi_params.get("smooth", 5)
    smo_type_rsi = rsi_params.get("smo_type_rsi", "RMA")
    smo_type_signal = rsi_params.get("smo_type_signal", "EMA")
    ob_value = float(rsi_params.get("ob_value", 80.0))
    os_value = float(rsi_params.get("os_value", 20.0))

    # ── Режим сигнала ─────────────────────────────────────────────────────────
    # "mean_reversion": входим когда уровень OB/OS покидается (против тренда)
    # "trend":          входим когда уровень OB/OS пробивается (по тренду)
    signal_mode: str = rsi_params.get("signal_mode", "mean_reversion")

    # ── Фильтр фантомных пересечений ─────────────────────────────────────────
    # Если ARSI ушёл дальше порога на entry_margin пунктов — сигнал отклоняется
    # (аналог entry_margin в live RSIStrategy; 0 = выключено)
    entry_margin = float(rsi_params.get("entry_margin", 0.0))

    # ── Управление позицией ───────────────────────────────────────────────────
    stop_ticks = int(rsi_params.get("stop_ticks", 80))
    take_profit_ticks = int(rsi_params.get("take_profit_ticks", 0))
    trailing_stop_ticks = int(rsi_params.get("trailing_stop_ticks", 0))
    breakeven_ticks = int(rsi_params.get("breakeven_ticks", 0))
    max_hold_minutes = int(rsi_params.get("max_hold_minutes", 150))

    stop_dist = stop_ticks * tick_size
    take_dist = take_profit_ticks * tick_size if take_profit_ticks > 0 else None
    trail_dist = trailing_stop_ticks * tick_size if trailing_stop_ticks > 0 else None
    breakeven_dist = breakeven_ticks * tick_size if breakeven_ticks > 0 else None
    max_hold_candles = max(1, max_hold_minutes // 5)

    # ── EOD-закрытие (принудительный выход в конце сессии) ───────────────────
    # Аналог eod_close_time в live-стратегии (position_manager.check_eod_close)
    eod_close_str: Optional[str] = rsi_params.get("eod_close_time")
    eod_close_min: Optional[int] = None
    if eod_close_str:
        _eh, _em = map(int, eod_close_str.split(":"))
        eod_close_min = _eh * 60 + _em

    # ── Торговые часы ─────────────────────────────────────────────────────────
    trading_hours = rsi_params.get("trading_hours", {})
    start_str = trading_hours.get("start", "10:05")
    end_str = trading_hours.get("end", "18:40")
    skip_first = int(rsi_params.get("skip_first_minutes", 5))
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    start_min = sh * 60 + sm + skip_first
    end_min = eh * 60 + em

    def in_trading_hours(ts: datetime) -> bool:
        msk = ts + timedelta(hours=MSK_OFFSET_HOURS)
        cur = msk.hour * 60 + msk.minute
        return start_min <= cur < end_min

    # ── Кулдауны ─────────────────────────────────────────────────────────────
    cooldown_sec = int(rsi_params.get("cooldown_seconds", 600))
    post_close_cd_sec = int(rsi_params.get("post_close_cooldown_seconds", 900))

    # ── ATR-фильтр ────────────────────────────────────────────────────────────
    atr_ratio_min = float(rsi_params.get("atr_ratio_min", 0.0))
    atr_short_len = int(rsi_params.get("atr_length_short", 5))
    atr_days = int(rsi_params.get("atr_days", 5))
    atr_long_window_candles = atr_days * 78  # ~78 5-min candles per trading day

    # ── RSI-индикатор ─────────────────────────────────────────────────────────
    rsi = AugmentedRSI(
        length=length,
        smooth=smooth,
        smo_type_rsi=smo_type_rsi,
        smo_type_signal=smo_type_signal,
    )

    trades: List[Dict] = []
    equity_curve: List[float] = []

    # ── Состояние позиции ─────────────────────────────────────────────────────
    in_position = False
    pos_direction: Optional[str] = None
    pos_entry_price: float = 0.0
    pos_entry_idx: int = 0
    pos_stop_price: float = 0.0
    pos_take_price: Optional[float] = None
    pos_peak_price: float = 0.0
    pos_stop_at_breakeven: bool = False

    # ── Состояние сигнала / кулдауна ──────────────────────────────────────────
    pending_signal: Optional[str] = None
    prev_arsi: Optional[float] = None
    last_entry_time: Optional[datetime] = None
    last_close_time: Optional[datetime] = None

    tr_history: List[float] = []

    for i, candle in enumerate(candles):
        tr = candle["high"] - candle["low"]
        tr_history.append(tr)

        # ── 1. Вход в позицию по open этой свечи (из pending_signal) ─────────
        if pending_signal is not None and not in_position:
            entry_price = candle["open"]
            in_position = True
            pos_direction = pending_signal
            pos_entry_price = entry_price
            pos_entry_idx = i
            pos_peak_price = entry_price
            pos_stop_at_breakeven = False

            if pos_direction == "long":
                pos_stop_price = entry_price - stop_dist
                pos_take_price = entry_price + take_dist if take_dist else None
            else:
                pos_stop_price = entry_price + stop_dist
                pos_take_price = entry_price - take_dist if take_dist else None

            pending_signal = None

        # ── 2. Обновление RSI ─────────────────────────────────────────────────
        res = rsi.update(candle["close"])

        # Прогрев: сохраняем prev_arsi, но не торгуем
        if i < warmup_candles:
            if res is not None:
                prev_arsi = res[0]
            continue

        if res is None:
            prev_arsi = None
            continue

        arsi, signal_line = res

        # ── 3. Управление открытой позицией ───────────────────────────────────
        if in_position:
            hold_candles = i - pos_entry_idx
            # Флаг: безубыток сработал именно на этой свече.
            # Если True — не проверяем стоп на этой же свече (баг "same-candle breakeven").
            breakeven_just_activated = False

            # Обновление трейлинг-стопа
            if trail_dist is not None:
                if pos_direction == "long" and candle["high"] > pos_peak_price:
                    pos_peak_price = candle["high"]
                    new_stop = pos_peak_price - trail_dist
                    if new_stop > pos_stop_price:
                        pos_stop_price = new_stop
                elif pos_direction == "short" and candle["low"] < pos_peak_price:
                    pos_peak_price = candle["low"]
                    new_stop = pos_peak_price + trail_dist
                    if new_stop < pos_stop_price:
                        pos_stop_price = new_stop

            # Безубыток (только при выключенном трейлинге)
            if trail_dist is None and breakeven_dist and not pos_stop_at_breakeven:
                if pos_direction == "long" and candle["high"] >= pos_entry_price + breakeven_dist:
                    pos_stop_price = pos_entry_price
                    pos_stop_at_breakeven = True
                    breakeven_just_activated = True
                elif pos_direction == "short" and candle["low"] <= pos_entry_price - breakeven_dist:
                    pos_stop_price = pos_entry_price
                    pos_stop_at_breakeven = True
                    breakeven_just_activated = True

            # Проверка условий выхода (пессимизм: стоп бьёт тейк если оба в одной свече)
            exit_reason: Optional[str] = None
            exit_price: float = 0.0

            if pos_direction == "long":
                # Стоп: не проверяем если безубыток только что активировался на этой свече
                if not breakeven_just_activated and candle["low"] <= pos_stop_price:
                    exit_price = pos_stop_price
                    exit_reason = (
                        "trailing_stop"
                        if trail_dist
                        else ("breakeven_stop" if pos_stop_at_breakeven else "stop_loss")
                    )
                elif pos_take_price is not None and candle["high"] >= pos_take_price:
                    exit_price = pos_take_price
                    exit_reason = "take_profit"
            else:  # short
                if not breakeven_just_activated and candle["high"] >= pos_stop_price:
                    exit_price = pos_stop_price
                    exit_reason = (
                        "trailing_stop"
                        if trail_dist
                        else ("breakeven_stop" if pos_stop_at_breakeven else "stop_loss")
                    )
                elif pos_take_price is not None and candle["low"] <= pos_take_price:
                    exit_price = pos_take_price
                    exit_reason = "take_profit"

            # Тайм-аут
            if exit_reason is None and hold_candles >= max_hold_candles:
                exit_price = candle["close"]
                exit_reason = "timeout"

            # EOD-закрытие (аналог position_manager.check_eod_close в live)
            if exit_reason is None and eod_close_min is not None:
                msk = candle["time"] + timedelta(hours=MSK_OFFSET_HOURS)
                cur_min = msk.hour * 60 + msk.minute
                if cur_min >= eod_close_min:
                    exit_price = candle["close"]
                    exit_reason = "eod_close"

            # Оформление закрытия
            if exit_reason is not None:
                if pos_direction == "long":
                    raw_pnl = (exit_price - pos_entry_price) * lot_size
                else:
                    raw_pnl = (pos_entry_price - exit_price) * lot_size
                commission = (pos_entry_price + exit_price) * lot_size * commission_rate
                pnl = raw_pnl - commission

                trades.append(
                    {
                        "direction": pos_direction,
                        "entry_time": candles[pos_entry_idx]["time"].isoformat(),
                        "exit_time": candle["time"].isoformat(),
                        "entry_time_msk": _msk_str(candles[pos_entry_idx]["time"]),
                        "exit_time_msk": _msk_str(candle["time"]),
                        "entry_price": round(pos_entry_price, 4),
                        "exit_price": round(exit_price, 4),
                        "exit_reason": exit_reason,
                        "pnl_rub": round(pnl, 2),
                        "hold_candles": hold_candles,
                    }
                )
                equity_curve.append(round(sum(t["pnl_rub"] for t in trades), 2))

                in_position = False
                last_close_time = candle["time"]
                prev_arsi = arsi
                continue

        # ── 4. Проверка сигнала входа (нет позиции, нет pending) ─────────────
        if not in_position and pending_signal is None and prev_arsi is not None:
            # Торговые часы
            if not in_trading_hours(candle["time"]):
                prev_arsi = arsi
                continue

            # Кулдаун от последнего входа
            if last_entry_time is not None:
                if (candle["time"] - last_entry_time).total_seconds() < cooldown_sec:
                    prev_arsi = arsi
                    continue

            # Кулдаун после закрытия позиции
            if last_close_time is not None:
                if (candle["time"] - last_close_time).total_seconds() < post_close_cd_sec:
                    prev_arsi = arsi
                    continue

            # ATR-фильтр
            if atr_ratio_min > 0 and len(tr_history) >= atr_short_len + 1:
                short_atr = sum(tr_history[-atr_short_len:]) / atr_short_len
                long_window = tr_history[-atr_long_window_candles:]
                long_atr = sum(long_window) / len(long_window)
                if long_atr > 0 and short_atr / long_atr < atr_ratio_min:
                    prev_arsi = arsi
                    continue

            # Детектирование пересечения
            sig: Optional[str] = None
            if signal_mode == "trend":
                # Трендовый: входим по направлению подтверждённого движения
                if prev_arsi < ob_value and arsi >= ob_value:
                    sig = "long"   # ARSI пересекает OB снизу вверх — рост подтверждён
                elif prev_arsi > os_value and arsi <= os_value:
                    sig = "short"  # ARSI пересекает OS сверху вниз — падение подтверждено
            else:
                # Mean-reversion (по умолчанию): входим против исчерпанного движения
                if prev_arsi < os_value and arsi >= os_value:
                    sig = "long"   # ARSI выходит из перепроданности
                elif prev_arsi > ob_value and arsi <= ob_value:
                    sig = "short"  # ARSI выходит из перекупленности

            # Фильтр фантомных пересечений (entry_margin)
            # Если ARSI ушёл слишком далеко от уровня за одну свечу — сигнал был "давно",
            # но мы его увидели с задержкой (аналог entry_margin в live RSIStrategy)
            if sig is not None and entry_margin > 0:
                if signal_mode == "trend":
                    if sig == "long" and arsi > ob_value + entry_margin:
                        sig = None
                    elif sig == "short" and arsi < os_value - entry_margin:
                        sig = None
                else:
                    if sig == "long" and arsi > os_value + entry_margin:
                        sig = None
                    elif sig == "short" and arsi < ob_value - entry_margin:
                        sig = None

            if sig is not None:
                pending_signal = sig
                last_entry_time = candle["time"]

        prev_arsi = arsi

    # ── Закрыть позицию оставшуюся в конце данных ─────────────────────────────
    if in_position and candles:
        last_c = candles[-1]
        exit_price = last_c["close"]
        if pos_direction == "long":
            raw_pnl = (exit_price - pos_entry_price) * lot_size
        else:
            raw_pnl = (pos_entry_price - exit_price) * lot_size
        commission = (pos_entry_price + exit_price) * lot_size * commission_rate
        pnl = raw_pnl - commission
        trades.append(
            {
                "direction": pos_direction,
                "entry_time": candles[pos_entry_idx]["time"].isoformat(),
                "exit_time": last_c["time"].isoformat(),
                "entry_time_msk": _msk_str(candles[pos_entry_idx]["time"]),
                "exit_time_msk": _msk_str(last_c["time"]),
                "entry_price": round(pos_entry_price, 4),
                "exit_price": round(exit_price, 4),
                "exit_reason": "end_of_data",
                "pnl_rub": round(pnl, 2),
                "hold_candles": len(candles) - 1 - pos_entry_idx,
            }
        )
        equity_curve.append(round(sum(t["pnl_rub"] for t in trades), 2))

    candles_used = max(0, len(candles) - warmup_candles)

    return {
        "ticker": ticker,
        "days": days,
        "candles_total": len(candles),
        "candles_used": candles_used,
        "trades": trades,
        "equity_curve": equity_curve,
        "metrics": _compute_metrics(trades),
        "signal_mode": signal_mode,
        "run_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M МСК"),
    }
