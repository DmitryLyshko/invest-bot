"""
Калькулятор OFI (Order Flow Imbalance — Дисбаланс потока ордеров).

OFI — это метрика, показывающая, кто агрессивнее: покупатели или продавцы.
Она рассчитывается на основе изменений стакана заявок (order book).

Логика: если на стороне bid (покупок) объём вырос или цена улучшилась,
значит покупательское давление усилилось (+1 вклад). Если на стороне ask
(продаж) объём упал или цена ухудшилась — тоже сигнал давления покупателей.
Итоговое значение нормируется от -1 до +1:
  +1.0 = максимальное давление покупателей
  -1.0 = максимальное давление продавцов
  ~0   = баланс / неопределённость
"""
import logging
import math
from collections import deque
from typing import Any, Dict, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OFICalculator:
    """
    Вычисляет OFI по изменениям лучших N уровней стакана.

    Параметры задаются через конфиг инструмента:
      - ofi_levels:       int — сколько уровней анализировать (обычно 3–10)
      - smooth_window:    int — окно сглаживания по скользящему среднему (обычно 5–20)
    """

    def __init__(self, ofi_levels: int, smooth_window: int = 1, ofi_scale: float = 1000.0) -> None:
        # Количество уровней стакана для анализа
        self.ofi_levels = ofi_levels

        # Размер окна сглаживания. При значении 1 — сглаживание отключено
        # (возвращается мгновенное значение, поведение как раньше).
        self.smooth_window = max(1, smooth_window)

        # Масштаб нормализации для tanh. Подбирается под типичный объём стакана
        # конкретного инструмента: при raw OFI ≈ ofi_scale → tanh ≈ 0.76.
        # Слишком большой scale → OFI всегда около 0, не достигает порога.
        # Слишком маленький scale → OFI всегда ±1, нет дифференциации.
        self.ofi_scale = max(1.0, ofi_scale)

        # Предыдущее состояние стакана — нужно для вычисления дельты
        self._prev_bids: List[Tuple[float, int]] = []  # [(price, qty), ...]
        self._prev_asks: List[Tuple[float, int]] = []

        # Скользящее окно последних значений OFI для сглаживания.
        # Стакан на ликвидных бумагах (SBER) обновляется несколько раз в секунду,
        # поэтому мгновенный OFI нестабилен — одна крупная заявка может перевернуть
        # знак. Среднее по N последним снимкам даёт более устойчивый сигнал.
        self._ofi_history: Deque[float] = deque(maxlen=self.smooth_window)

        # Последнее рассчитанное (уже сглаженное) значение OFI
        self._last_ofi: Optional[float] = None

    def update(self, bids: List[Tuple[float, int]], asks: List[Tuple[float, int]]) -> Optional[float]:
        """
        Принять новый снапшот стакана и вернуть значение OFI.

        Возвращает None при первом вызове (нет предыдущего состояния для сравнения).

        bids — отсортированы по убыванию цены (лучший bid первый)
        asks — отсортированы по возрастанию цены (лучший ask первый)
        """
        # Берём только топ N уровней для анализа
        curr_bids = bids[: self.ofi_levels]
        curr_asks = asks[: self.ofi_levels]

        if not self._prev_bids or not self._prev_asks:
            # Первый снапшот — запоминаем и ждём следующего
            self._prev_bids = curr_bids
            self._prev_asks = curr_asks
            return None

        # Вычисляем вклад от стороны покупателей (bids)
        bid_ofi = self._compute_side_ofi(self._prev_bids, curr_bids, side="bid")

        # Вычисляем вклад от стороны продавцов (asks)
        ask_ofi = self._compute_side_ofi(self._prev_asks, curr_asks, side="ask")

        # Суммарный поток: bid_ofi отражает давление покупателей,
        # ask_ofi — давление продавцов (знак инвертируется при сложении)
        total_flow = bid_ofi - ask_ofi

        # Нормализуем итоговое значение в диапазон [-1, 1].
        # Максимально возможный вклад с каждой стороны = ofi_levels × MAX_MULTIPLIER.
        # Мы используем нормализацию через максимум наблюдённых значений
        # (adaptive), но для простоты — через заданный масштаб.
        ofi_normalized = self._normalize(total_flow)

        # Сглаживаем OFI по скользящему окну чтобы убрать краткосрочный шум стакана.
        # Добавляем текущее мгновенное значение в историю и возвращаем среднее.
        # Если история ещё не заполнена — берём среднее по имеющимся значениям,
        # что безопаснее чем возвращать None или ждать заполнения окна.
        self._ofi_history.append(ofi_normalized)
        smoothed_ofi = sum(self._ofi_history) / len(self._ofi_history)

        # Сохраняем текущее состояние как предыдущее для следующего тика
        self._prev_bids = curr_bids
        self._prev_asks = curr_asks
        self._last_ofi = smoothed_ofi

        return smoothed_ofi

    def _compute_side_ofi(
        self,
        prev_levels: List[Tuple[float, int]],
        curr_levels: List[Tuple[float, int]],
        side: str,
    ) -> float:
        """
        Вычислить вклад OFI для одной стороны стакана (bid или ask).

        Алгоритм основан на статье Cont, Kukanov, Stoikov (2014):
        для каждого уровня смотрим изменение объёма с учётом движения цены.

        Правила для BID-стороны:
          - Если цена bid выросла (лучший bid улучшился): добавляем +curr_qty
          - Если цена bid упала (лучший bid ухудшился): вычитаем prev_qty
          - Если цена та же: добавляем разницу (curr_qty - prev_qty)

        Для ASK-стороны симметрично.
        """
        ofi = 0.0

        # Приводим оба списка к одинаковой длине
        n = min(len(prev_levels), len(curr_levels), self.ofi_levels)
        if n == 0:
            return 0.0

        for i in range(n):
            prev_price, prev_qty = prev_levels[i]
            curr_price, curr_qty = curr_levels[i]

            if side == "bid":
                # Bid-сторона: положительный знак означает давление покупателей
                if curr_price > prev_price:
                    # Покупатели улучшили цену — агрессивный спрос
                    ofi += curr_qty
                elif curr_price < prev_price:
                    # Покупатели отступили — убираем их объём
                    ofi -= prev_qty
                else:
                    # Цена та же — смотрим изменение объёма
                    ofi += curr_qty - prev_qty
            else:
                # Ask-сторона: отрицательный знак означает давление продавцов
                if curr_price < prev_price:
                    # Продавцы улучшили свою цену — агрессивное предложение
                    ofi += curr_qty
                elif curr_price > prev_price:
                    # Продавцы отступили — убираем их объём
                    ofi -= prev_qty
                else:
                    ofi += curr_qty - prev_qty

        return ofi

    def _normalize(self, raw_ofi: float) -> float:
        """
        Нормализовать сырой OFI в диапазон [-1, 1] через tanh.

        tanh хорошо работает как нормализатор для потоков:
        - медленно насыщается при больших значениях
        - не обрезает данные резко
        - сохраняет знак

        Масштабирующий коэффициент 1000 подобран эмпирически для
        типичного дневного объёма SBER. При работе с другими инструментами
        можно вынести в конфиг.
        """
        return math.tanh(raw_ofi / self.ofi_scale)

    @property
    def last_ofi(self) -> Optional[float]:
        """Последнее рассчитанное значение OFI (без пересчёта)."""
        return self._last_ofi

    def reset(self) -> None:
        """Сбросить состояние — используется при переподключении стрима."""
        self._prev_bids = []
        self._prev_asks = []
        self._ofi_history.clear()
        self._last_ofi = None
