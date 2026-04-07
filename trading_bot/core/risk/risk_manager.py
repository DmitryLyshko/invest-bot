"""
Риск-менеджер.

Проверяет разрешение на торговлю перед КАЖДЫМ ордером.
Ни один ордер не должен быть выставлен без прохождения всех проверок.

Проверки выполняются в порядке строгости — самые дешёвые первыми.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from trading_bot.config import settings
from trading_bot.db import repository

logger = logging.getLogger(__name__)


class RiskCheckFailed(Exception):
    """Выбрасывается при отказе риск-менеджера."""
    pass


class RiskManager:
    """
    Централизованная проверка рисков.

    Все проверки выполняются синхронно и бросают RiskCheckFailed при отказе.
    Каждый отказ логируется в БД.
    """

    def __init__(self, instrument_id: int, instrument_config: dict) -> None:
        self.instrument_id = instrument_id
        self.params = instrument_config
        self._daily_loss_limit = settings.DAILY_LOSS_LIMIT_RUB

    def check_all(
        self,
        signal_type: str,
        has_open_position: bool,
        current_position_direction: Optional[str] = None,
    ) -> None:
        """
        Выполнить все проверки рисков.

        Бросает RiskCheckFailed с описанием причины если хотя бы одна проверка не прошла.

        signal_type             — "long" / "short" / "exit"
        has_open_position       — есть ли уже открытая позиция
        current_position_direction — направление открытой позиции (если есть)
        """
        self._check_bot_active()
        self._check_trading_hours()
        self._check_no_pyramiding(signal_type, has_open_position, current_position_direction)
        self._check_daily_loss_limit()

    def _check_bot_active(self) -> None:
        """
        Проверить, включён ли бот глобально.
        Можно отключить из веб-интерфейса без перезапуска процесса.
        """
        is_active = repository.get_bot_active()
        if not is_active:
            self._deny("bot_inactive", "Бот деактивирован через веб-интерфейс")

    def _check_trading_hours(self) -> None:
        """
        Проверить торговые часы по московскому времени.

        Дублирование проверки из стратегии намеренно:
        risk_manager — последняя линия защиты перед реальным ордером.
        """
        now_utc = datetime.utcnow()
        now_msk = now_utc + timedelta(hours=3)
        current_time = now_msk.time()

        from datetime import time
        hours = self.params.get("trading_hours", {})
        start_str = hours.get("start", "10:05")
        end_str = hours.get("end", "18:30")

        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))

        if not (time(sh, sm) <= current_time <= time(eh, em)):
            self._deny(
                "outside_trading_hours",
                f"Текущее время MSK {current_time.strftime('%H:%M')} вне торгового окна {start_str}-{end_str}",
            )

    def _check_no_pyramiding(
        self,
        signal_type: str,
        has_open_position: bool,
        current_position_direction: Optional[str],
    ) -> None:
        """
        Запрет пирамидинга — открытия новой позиции поверх существующей.

        Разрешено:
        - Открыть позицию если нет открытой (long/short при has_open_position=False)
        - Закрыть существующую позицию (exit при has_open_position=True)
        - Открыть противоположную позицию после закрытия текущей

        Запрещено:
        - Открыть новую позицию при уже открытой в том же направлении
        """
        if signal_type in ("long", "short") and has_open_position:
            self._deny(
                "pyramiding_blocked",
                f"Попытка открыть {signal_type} при уже открытой позиции {current_position_direction}. "
                "Пирамидинг запрещён.",
            )

        if signal_type == "exit" and not has_open_position:
            self._deny(
                "no_position_to_close",
                "Сигнал exit при отсутствии открытой позиции",
            )

    def _check_daily_loss_limit(self) -> None:
        """
        Проверить дневной лимит убытков.

        Если суммарный P&L за сегодня достиг или превысил лимит —
        прекращаем торговлю до следующего дня. Защита от серии убытков.
        """
        today_pnl = repository.get_today_pnl(self.instrument_id)

        if today_pnl <= self._daily_loss_limit:
            self._deny(
                "daily_loss_limit",
                f"Дневной лимит убытков достигнут: {today_pnl:.2f} руб. "
                f"(лимит: {self._daily_loss_limit:.2f} руб.)",
            )

    def _deny(self, reason: str, message: str) -> None:
        """Логировать отказ и бросить исключение."""
        full_msg = f"[{reason}] {message}"
        logger.warning(f"RiskManager отказал: {full_msg}")
        repository.log_event("WARNING", "risk_manager", full_msg)
        raise RiskCheckFailed(full_msg)
