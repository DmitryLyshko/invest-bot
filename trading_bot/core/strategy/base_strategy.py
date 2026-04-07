"""
Абстрактный базовый класс для всех торговых стратегий.
Все конкретные стратегии должны наследоваться от BaseStrategy
и реализовывать методы on_orderbook и on_trade.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class SignalType(str, Enum):
    LONG = "long"
    SHORT = "short"
    EXIT = "exit"


class SignalReason(str, Enum):
    COMBO_TRIGGERED = "combo_triggered"
    OFI_REVERSED = "ofi_reversed"
    TIMEOUT = "timeout"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    MANUAL = "manual"


@dataclass
class Signal:
    """Объект сигнала, возвращаемый стратегией."""
    signal_type: SignalType
    reason: SignalReason
    ofi_value: Optional[float] = None
    print_volume: Optional[float] = None
    print_side: Optional[str] = None  # "buy" / "sell"
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __str__(self) -> str:
        return (
            f"Signal({self.signal_type.value}, reason={self.reason.value}, "
            f"ofi={f'{self.ofi_value:.3f}' if self.ofi_value is not None else 'N/A'})"
        )


class BaseStrategy(ABC):
    """
    Базовый класс стратегии.

    Контракт:
    - Получает рыночные данные через on_orderbook() и on_trade()
    - Возвращает объект Signal или None через get_signal()
    - Параметры инструмента загружаются через load_params()
    """

    def __init__(self, instrument_config: Dict[str, Any]) -> None:
        self.params: Dict[str, Any] = {}
        self.load_params(instrument_config)

    def load_params(self, instrument_config: Dict[str, Any]) -> None:
        """
        Загрузить параметры из конфигурации инструмента.
        Вызывается при инициализации и при обновлении конфига.
        """
        self.params = dict(instrument_config)

    @abstractmethod
    def on_orderbook(self, orderbook_data: Dict[str, Any]) -> None:
        """
        Обработать обновление стакана заявок.

        orderbook_data должен содержать:
          - figi: str
          - bids: list of (price, quantity)  — покупки, сортированы desc
          - asks: list of (price, quantity)  — продажи, сортированы asc
          - time: datetime
        """
        ...

    @abstractmethod
    def on_trade(self, trade_data: Dict[str, Any]) -> None:
        """
        Обработать сделку из стрима.

        trade_data должен содержать:
          - figi: str
          - price: float
          - quantity: int  (в лотах)
          - direction: str  "buy" | "sell" | "unknown"
          - time: datetime
        """
        ...

    @abstractmethod
    def get_signal(self) -> Optional[Signal]:
        """
        Вернуть текущий накопленный сигнал или None.
        После возврата сигнала должен быть сброшен (чтобы не повторять его).
        """
        ...

    def reset(self) -> None:
        """Сбросить внутреннее состояние стратегии."""
        pass
