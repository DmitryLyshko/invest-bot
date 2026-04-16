"""
Оптимизатор параметров RSI-стратегии.
Grid search по пространству параметров — выбирает наилучший конфиг по profit_factor.
Результат НЕ применяется автоматически: пользователь жмёт «Применить» в UI.
"""
import logging
from copy import deepcopy
from itertools import product
from typing import Any, Callable, Dict, List, Optional

from trading_bot.backtest.engine import run_backtest

logger = logging.getLogger(__name__)

# ── Сетка перебора ────────────────────────────────────────────────────────────
DEFAULT_GRID: Dict[str, List] = {
    "ob_value":      [75.0, 80.0, 85.0],   # уровень перекупленности
    "os_value":      [15.0, 20.0, 25.0],   # уровень перепроданности
    "stop_mult":     [0.7,  1.0,  1.4],    # × текущий stop_ticks
    "take_ratio":    [2.0,  3.0,  4.0],    # × stop_ticks
    "trail_ratio":   [0.0,  0.5,  0.7],    # × stop_ticks (0 = трейлинг выкл)
    "atr_ratio_min": [0.0,  0.5,  0.7],    # порог ATR-фильтра
}

MIN_TRADES_DEFAULT = 10   # минимум сделок для учёта результата
TOP_N = 10                # сколько лучших конфигов хранить


def total_combos(grid: Optional[Dict] = None) -> int:
    """Общее число комбинаций в сетке (с учётом фильтра os < ob)."""
    if grid is None:
        grid = DEFAULT_GRID
    count = 1
    for v in grid.values():
        count *= len(v)
    # Примерный вычет невалидных пар os >= ob: при одинаковых размерах os/ob ~1/3 отсевается
    return count


def optimize_ticker(
    candles: List[Dict],
    rsi_params_base: Dict[str, Any],
    instrument_params: Dict[str, Any],
    warmup_candles: int = 300,
    grid: Optional[Dict] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    min_trades: int = MIN_TRADES_DEFAULT,
) -> List[Dict]:
    """
    Перебор параметров RSI-стратегии по сетке.

    Args:
        candles:            список свечей из candle_loader.load_candles()
        rsi_params_base:    текущая секция из rsi_config.yaml (базовые значения)
        instrument_params:  {ticker, lot_size, tick_size, commission_rate}
        warmup_candles:     число свечей на прогрев RMA
        grid:               сетка перебора (по умолчанию DEFAULT_GRID)
        progress_cb:        callback(done, total) для отчёта прогресса
        min_trades:         минимум сделок, чтобы результат учитывался

    Returns:
        Список top-N конфигов, отсортированных по profit_factor убыванию.
        Каждый элемент: {"params": {...}, "metrics": {...}}.
        Ключи params — те же, что в rsi_config.yaml.
    """
    if grid is None:
        grid = DEFAULT_GRID

    base_stop = int(rsi_params_base.get("stop_ticks", 80))

    keys = list(grid.keys())
    values_list = list(grid.values())
    combos = list(product(*values_list))
    total = len(combos)

    results: List[Dict] = []

    for i, combo in enumerate(combos):
        if progress_cb:
            progress_cb(i, total)

        p = dict(zip(keys, combo))

        # os_value должен быть строго меньше ob_value
        if p["os_value"] >= p["ob_value"]:
            continue

        stop = max(5, round(base_stop * p["stop_mult"]))
        take = round(stop * p["take_ratio"]) if p["take_ratio"] > 0 else 0
        trail = round(stop * p["trail_ratio"]) if p["trail_ratio"] > 0 else 0
        # breakeven актуален только когда трейлинг выкл
        breakeven = round(stop * 0.85) if trail == 0 and stop > 5 else 0

        params = deepcopy(rsi_params_base)
        params["ob_value"]            = p["ob_value"]
        params["os_value"]            = p["os_value"]
        params["stop_ticks"]          = stop
        params["take_profit_ticks"]   = take
        params["trailing_stop_ticks"] = trail
        params["breakeven_ticks"]     = breakeven
        params["atr_ratio_min"]       = p["atr_ratio_min"]

        try:
            bt = run_backtest(
                candles=candles,
                rsi_params=params,
                instrument_params=instrument_params,
                warmup_candles=warmup_candles,
            )
        except Exception as exc:
            logger.warning("optimizer: run_backtest error combo %s: %s", p, exc)
            continue

        m = bt["metrics"]
        if m["n_trades"] < min_trades:
            continue

        results.append({
            "params": {
                "ob_value":            p["ob_value"],
                "os_value":            p["os_value"],
                "stop_ticks":          stop,
                "take_profit_ticks":   take,
                "trailing_stop_ticks": trail,
                "breakeven_ticks":     breakeven,
                "atr_ratio_min":       p["atr_ratio_min"],
            },
            "metrics": m,
        })

    if progress_cb:
        progress_cb(total, total)

    results.sort(
        key=lambda x: (x["metrics"]["profit_factor"], x["metrics"]["n_trades"]),
        reverse=True,
    )
    return results[:TOP_N]
