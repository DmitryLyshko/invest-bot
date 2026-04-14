"""
Telegram-уведомления для торгового бота.

Отправляет сообщения асинхронно (в фоновом потоке),
не блокируя торговую логику.

Включается через .env: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
Если переменные не заданы — все вызовы no-op.
"""
import logging
import queue
import threading
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Отправляет уведомления в Telegram-чат.

    Все методы send_* неблокирующие — отправка в фоновом daemon-потоке.
    При ошибке сети только логирует WARNING, не бросает исключений.
    """

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(token and chat_id)
        self._queue: queue.Queue[str] = queue.Queue()
        if self._enabled:
            logger.info("Telegram уведомления включены (chat_id=%s)", chat_id)
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()
        else:
            logger.info("Telegram уведомления отключены (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы)")

    # ── Публичные методы ────────────────────────────────────────────────────────

    def send_bot_started(self, tickers: list[str], sandbox: bool) -> None:
        """Уведомление о запуске бота."""
        mode = "SANDBOX" if sandbox else "РЕАЛЬНАЯ ТОРГОВЛЯ"
        tickers_str = ", ".join(tickers)
        self._send(
            f"🤖 <b>Бот запущен</b>\n"
            f"Режим: {mode}\n"
            f"Тикеры: {tickers_str}"
        )

    def send_trading_day_started(self, tickers: list[str]) -> None:
        """Уведомление о начале торговой сессии (вызывается по расписанию в 10:05 МСК)."""
        now_msk = datetime.utcnow() + timedelta(hours=3)
        date_str = now_msk.strftime("%d.%m.%Y")
        tickers_str = ", ".join(tickers)
        self._send(
            f"📈 <b>Торговая сессия началась</b>\n"
            f"Дата: {date_str}\n"
            f"Торгуем: {tickers_str}\n"
            f"Время: 10:05–18:30 МСК"
        )

    def send_position_opened(
        self,
        ticker: str,
        direction: str,
        entry_price: float,
        quantity_lots: int,
        lot_size: int,
    ) -> None:
        """Уведомление об открытии позиции."""
        dir_icon = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        shares = quantity_lots * lot_size
        self._send(
            f"📊 <b>Открыта позиция {ticker}</b>\n"
            f"Направление: {dir_icon}\n"
            f"Цена: {entry_price:.2f} ₽\n"
            f"Объём: {quantity_lots} лот(ов) / {shares} акций"
        )

    def send_position_closed(
        self,
        ticker: str,
        direction: str,
        entry_price: float,
        close_price: float,
        quantity_lots: int,
        lot_size: int,
        pnl: float,
        hold_seconds: int,
        exit_reason: str,
    ) -> None:
        """Уведомление о закрытии позиции с результатом."""
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_icon = "✅" if pnl >= 0 else "❌"
        hold_str = _format_hold(hold_seconds)
        reason_str = _format_reason(exit_reason)
        shares = quantity_lots * lot_size
        dir_str = "LONG" if direction == "long" else "SHORT"
        self._send(
            f"{pnl_icon} <b>Позиция {ticker} закрыта</b>\n"
            f"Направление: {dir_str}\n"
            f"Вход: {entry_price:.2f} → Выход: {close_price:.2f} ₽\n"
            f"Объём: {quantity_lots} лот(ов) / {shares} акций\n"
            f"P&L: <b>{pnl_sign}{pnl:.2f} ₽</b>\n"
            f"Причина: {reason_str}\n"
            f"Удержание: {hold_str}"
        )

    def send_position_recovered(
        self,
        ticker: str,
        strategy_name: str,
        direction: str,
        entry_price: float,
        quantity_lots: int,
        open_at: "datetime",
    ) -> None:
        """Уведомление о восстановлении позиции после рестарта."""
        dir_icon = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        open_msk = open_at + timedelta(hours=3)
        self._send(
            f"⚡ <b>Позиция восстановлена после рестарта</b>\n"
            f"Тикер: <b>{ticker}</b> [{strategy_name}]\n"
            f"Направление: {dir_icon}\n"
            f"Цена входа: {entry_price:.2f} ₽\n"
            f"Объём: {quantity_lots} лот(ов)\n"
            f"Открыта: {open_msk.strftime('%H:%M:%S')} МСК"
        )

    # ── Внутренние методы ───────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Поставить сообщение в очередь отправки."""
        if not self._enabled:
            return
        self._queue.put(text)

    def _worker(self) -> None:
        """Фоновый поток: читает очередь и отправляет с 3 попытками."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        while True:
            text = self._queue.get()
            for attempt in range(3):
                try:
                    resp = requests.post(
                        url,
                        json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                        timeout=10,
                    )
                    if resp.ok:
                        break
                    # 429 Too Many Requests — ждём retry_after
                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                        logger.warning("Telegram: rate limit, ждём %s сек", retry_after)
                        time.sleep(retry_after)
                    else:
                        logger.warning("Telegram: ошибка отправки %s — %s", resp.status_code, resp.text[:200])
                        break
                except Exception as exc:
                    logger.warning("Telegram: попытка %d/3 провалилась: %s", attempt + 1, exc)
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            self._queue.task_done()


# ── Вспомогательные функции ─────────────────────────────────────────────────

def _format_hold(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours} ч {mins} мин"


def _format_reason(reason: str) -> str:
    mapping = {
        "ofi_reversed": "OFI-разворот",
        "timeout": "тайм-аут",
        "stop_loss": "стоп-лосс",
        "breakeven_stop": "безубыток",
        "take_profit": "тейк-профит",
        "trailing_stop": "трейлинг-стоп",
        "manual": "ручное закрытие",
    }
    return mapping.get(reason, reason)
