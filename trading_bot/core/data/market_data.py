"""
Утилиты для нормализации рыночных данных из T-Invest API.

T-Invest API отдаёт данные в специфических форматах (Quotation, MoneyValue и т.д.).
Этот модуль нормализует их в стандартные Python-типы перед передачей в стратегию.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from tinkoff.invest import (
    OrderBook,
    Trade as TinkoffTrade,
    Quotation,
)
from tinkoff.invest.utils import quotation_to_decimal


def quotation_to_float(q: Optional[Quotation]) -> float:
    if q is None:
        return 0.0
    return float(quotation_to_decimal(q))


def normalize_orderbook(ob: OrderBook) -> Dict[str, Any]:
    """
    Преобразовать OrderBook из T-Invest в нормализованный словарь.

    Возвращает:
      {
        "figi": str,
        "bids": [(price, qty), ...],  # отсортированы по убыванию цены
        "asks": [(price, qty), ...],  # отсортированы по возрастанию цены
        "time": datetime,
      }
    """
    bids: List[Tuple[float, int]] = [
        (quotation_to_float(level.price), level.quantity)
        for level in ob.bids
    ]
    asks: List[Tuple[float, int]] = [
        (quotation_to_float(level.price), level.quantity)
        for level in ob.asks
    ]

    # T-Invest уже возвращает bids desc и asks asc, но сортируем явно для надёжности
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    return {
        "figi": ob.figi,
        "bids": bids,
        "asks": asks,
        "time": ob.time if hasattr(ob, "time") else datetime.utcnow(),
    }


def normalize_trade(trade: TinkoffTrade) -> Dict[str, Any]:
    """
    Преобразовать Trade из T-Invest в нормализованный словарь.

    direction_map:
      1 → "buy"   (TRADE_DIRECTION_BUY)
      2 → "sell"  (TRADE_DIRECTION_SELL)
      0 → "unknown"
    """
    direction_map = {0: "unknown", 1: "buy", 2: "sell"}
    return {
        "figi": trade.figi,
        "price": quotation_to_float(trade.price),
        "quantity": trade.quantity,  # в лотах
        "direction": direction_map.get(trade.direction, "unknown"),
        "time": trade.time if hasattr(trade, "time") else datetime.utcnow(),
    }


def get_spread(orderbook: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Вернуть (bid, ask) — лучшие цены из стакана."""
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])
    if not bids or not asks:
        return None
    return bids[0][0], asks[0][0]


def get_mid_price(orderbook: Dict[str, Any]) -> Optional[float]:
    """Вернуть среднюю цену между bid и ask."""
    spread = get_spread(orderbook)
    if spread is None:
        return None
    return (spread[0] + spread[1]) / 2
