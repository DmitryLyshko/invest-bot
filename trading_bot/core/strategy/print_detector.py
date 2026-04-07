"""
Детектор крупных принтов (Large Print Detector).

"Принт" в трейдинге — это крупная сделка, выбивающаяся из обычного потока.
Крупные принты важны, потому что они часто инициируются маркет-мейкерами
или институциональными игроками и могут предвосхищать движение цены.

Логика определения:
  1. Ведём скользящее окно последних N сделок
  2. Считаем медианный объём в окне
  3. Если объём новой сделки >= медиана × мультипликатор → это крупный принт
  4. Определяем сторону агрессора: кто был инициатором — покупатель или продавец

Медиана используется вместо среднего, т.к. сама по себе нечувствительна к
выбросам — крупные принты не "загрязняют" базовую метрику.
"""
import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PrintEvent:
    """Описание обнаруженного крупного принта."""
    price: float
    volume: float        # объём в лотах
    side: str            # "buy" — агрессор-покупатель, "sell" — агрессор-продавец
    multiplier: float    # во сколько раз объём превысил медиану
    timestamp: datetime


class PrintDetector:
    """
    Определяет крупные принты на основе объёма сделок.

    Параметры:
      print_window     — размер скользящего окна (сколько сделок хранить)
      print_multiplier — во сколько раз объём должен превышать медиану
    """

    def __init__(self, print_window: int, print_multiplier: float) -> None:
        self.print_window = print_window
        self.print_multiplier = print_multiplier

        # deque с ограниченным размером автоматически вытесняет старые элементы
        # Храним только объёмы для расчёта медианы — эффективно по памяти
        self._volume_window: Deque[float] = deque(maxlen=print_window)

        # Последний обнаруженный крупный принт
        self._last_print: Optional[PrintEvent] = None

        # Последние известные цены bid и ask — нужны для определения агрессора
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None

    def update_quotes(self, bid: float, ask: float) -> None:
        """
        Обновить текущие котировки из стакана.
        Вызывать при каждом обновлении стакана, до on_trade().
        """
        self._last_bid = bid
        self._last_ask = ask

    def on_trade(
        self,
        price: float,
        volume: float,
        direction: str,
        timestamp: datetime,
    ) -> Optional[PrintEvent]:
        """
        Обработать новую сделку.

        Возвращает PrintEvent если сделка является крупным принтом, иначе None.

        direction — направление из T-Invest API:
          "buy"  — сделка прошла по ask (маркет-бай)
          "sell" — сделка прошла по bid (маркет-сел)
          "unknown" — направление неизвестно, определяем по цене
        """
        # Сначала добавляем объём в окно — медиана всегда считается по историческим данным,
        # не включая текущую сделку. Это предотвращает "самоссылку".
        current_volumes = list(self._volume_window)

        # Добавляем текущий объём в окно для следующих расчётов
        self._volume_window.append(volume)

        # Если окно ещё не заполнено до минимального порога — сигнал ненадёжен
        if len(current_volumes) < max(10, self.print_window // 10):
            # Ждём накопления хотя бы 10% окна или минимум 10 точек
            return None

        # Считаем медиану объёмов в окне
        median_volume = statistics.median(current_volumes)

        # Сравниваем объём текущей сделки с медианой
        if median_volume <= 0:
            return None

        ratio = volume / median_volume

        if ratio < self.print_multiplier:
            # Обычная сделка — не крупный принт
            return None

        # Определяем сторону агрессора
        side = self._determine_aggressor_side(price, direction)

        print_event = PrintEvent(
            price=price,
            volume=volume,
            side=side,
            multiplier=round(ratio, 1),
            timestamp=timestamp,
        )
        self._last_print = print_event

        logger.debug(
            f"Крупный принт: {volume:.0f} лотов @ {price:.2f} "
            f"(медиана: {median_volume:.1f}, x{ratio:.1f}), сторона: {side}"
        )

        return print_event

    def _determine_aggressor_side(self, trade_price: float, direction: str) -> str:
        """
        Определить, кто был агрессором в сделке: покупатель или продавец.

        Метод 1 (приоритет): использовать direction из T-Invest API.
        Метод 2 (fallback): сравнить цену сделки с bid/ask из последнего стакана.

        Правило tick-test (упрощённое):
          - Цена >= ask → агрессор-покупатель (hit the ask)
          - Цена <= bid → агрессор-продавец (hit the bid)
          - Между bid и ask → неопределённо, используем direction или "unknown"
        """
        # Если T-Invest уже дал нам направление — доверяем ему
        if direction in ("buy", "sell"):
            return direction

        # Fallback: определяем по положению цены в спреде
        if self._last_bid is not None and self._last_ask is not None:
            if trade_price >= self._last_ask:
                return "buy"   # покупатель пошёл на ask
            elif trade_price <= self._last_bid:
                return "sell"  # продавец пошёл на bid

        # Если ничего не знаем — помечаем как неизвестно
        return "unknown"

    @property
    def last_print(self) -> Optional[PrintEvent]:
        """Последний обнаруженный крупный принт (или None)."""
        return self._last_print

    def clear_last_print(self) -> None:
        """Сбросить последний принт после обработки сигнала."""
        self._last_print = None

    @property
    def window_filled(self) -> bool:
        """Достаточно ли данных для надёжного определения принтов."""
        return len(self._volume_window) >= max(10, self.print_window // 10)

    @property
    def current_median_volume(self) -> Optional[float]:
        """Текущая медиана объёмов (для отладки и мониторинга)."""
        if len(self._volume_window) < 2:
            return None
        return statistics.median(self._volume_window)

    def reset(self) -> None:
        """Сбросить состояние — используется при переподключении стрима."""
        self._volume_window.clear()
        self._last_print = None
        self._last_bid = None
        self._last_ask = None
