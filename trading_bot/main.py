"""
Точка входа торгового бота.

Порядок инициализации:
1. Настройка логирования
2. Инициализация БД
3. Загрузка конфигурации инструментов
4. Получение account_id из T-Invest API
5. Создание компонентов (стратегия, order_manager, position_manager)
6. Запуск стрима в отдельном потоке
7. Запуск веб-дашборда в отдельном потоке
8. Запуск планировщика (тайм-аут позиции)
"""
import logging
import signal
import sys
import threading
from typing import Dict, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from tinkoff.invest import Client
from tinkoff.invest.sandbox.client import SandboxClient

from trading_bot.config import settings
from trading_bot.core.data.data_recorder import DataRecorder
from trading_bot.core.data.stream_handler import StreamHandler
from trading_bot.core.execution.order_manager import OrderManager
from trading_bot.core.execution.position_manager import PositionManager
from trading_bot.core.strategy.combo_strategy import ComboStrategy
from trading_bot.db import repository
from trading_bot.web.app import create_app, set_position_manager
from trading_bot.web.auth import ensure_default_user


def setup_logging() -> None:
    """Настроить логирование: файл + консоль."""
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.handlers.RotatingFileHandler(
                settings.LOG_FILE,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )


def load_instruments_config() -> dict:
    """Загрузить конфигурацию инструментов из YAML."""
    with open(settings.INSTRUMENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sync_instruments_to_db(config: dict) -> Dict[str, dict]:
    """
    Синхронизировать конфиг инструментов с БД.
    Возвращает словарь {ticker: {instrument_id, ...params}}
    """
    logger = logging.getLogger(__name__)
    result = {}

    for ticker, params in config.items():
        db_record = repository.upsert_instrument({
            "ticker": ticker,
            "figi": params["figi"],
            "lot_size": params["lot_size"],
            "ofi_threshold": params.get("ofi_threshold", 0.6),
            "print_multiplier": params.get("print_multiplier", 7.0),
            "print_window": params.get("print_window", 200),
            "ofi_levels": params.get("ofi_levels", 5),
            "cooldown_seconds": params.get("cooldown_seconds", 60),
            "max_hold_minutes": params.get("max_hold_minutes", 60),
            "stop_ticks": params.get("stop_ticks", 3),
            "is_active": True,
        })
        result[ticker] = {**params, "db_instrument_id": db_record.id}
        logger.info(f"Синхронизирован инструмент: {ticker} (id={db_record.id})")

    return result


def get_first_account_id() -> str:
    """Получить первый доступный счёт из T-Invest API."""
    logger = logging.getLogger(__name__)
    if settings.USE_SANDBOX:
        with SandboxClient(settings.TINKOFF_TOKEN) as client:
            response = client.sandbox.get_sandbox_accounts()
            if not response.accounts:
                new_account = client.sandbox.open_sandbox_account()
                logger.info(f"Создан sandbox счёт: id={new_account.account_id}")
                return new_account.account_id
            account = response.accounts[0]
            logger.info(f"Используется sandbox счёт: id={account.id}")
            return account.id
    else:
        with Client(settings.TINKOFF_TOKEN) as client:
            response = client.users.get_accounts()
            if not response.accounts:
                raise RuntimeError("Нет доступных счётов в T-Invest")
            account = response.accounts[0]
            logger.info(f"Используется счёт: id={account.id}, name={account.name}")
            return account.id


def build_components(ticker: str, instrument_params: dict, account_id: str):
    """Создать все торговые компоненты для одного инструмента."""
    instrument_id = instrument_params["db_instrument_id"]

    strategy = ComboStrategy(instrument_params)
    order_manager = OrderManager(account_id, instrument_id)
    position_manager = PositionManager(
        instrument_id=instrument_id,
        instrument_config=instrument_params,
        order_manager=order_manager,
        strategy=strategy,
    )

    return strategy, order_manager, position_manager


def make_event_handlers(
    strategy: ComboStrategy,
    position_manager: PositionManager,
    recorder: DataRecorder,
):
    """
    Создать callback-функции для стрима.
    Возвращает (on_orderbook, on_trade).
    """
    def on_orderbook(data: dict) -> None:
        recorder.on_orderbook(data)
        strategy.on_orderbook(data)
        sig = strategy.get_signal()
        if sig is not None:
            position_manager.on_signal(sig)

    def on_trade(data: dict) -> None:
        recorder.on_trade(data)
        strategy.on_trade(data)
        position_manager.update_market_price(data["price"])
        # После on_trade тоже проверяем сигнал (PrintDetector мог сработать)
        sig = strategy.get_signal()
        if sig is not None:
            position_manager.on_signal(sig)

    return on_orderbook, on_trade


def run_web(app) -> None:
    app.run(host=settings.WEB_HOST, port=settings.WEB_PORT, use_reloader=False)


def main() -> None:
    import logging.handlers  # нужен для RotatingFileHandler

    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=== Trading Bot запускается ===")
    logger.info(f"Sandbox: {settings.USE_SANDBOX}")

    # ── Инициализация БД ──────────────────────────────────────────────────────
    repository.init_db()
    logger.info("БД инициализирована")

    # ── Пользователь по умолчанию ─────────────────────────────────────────────
    ensure_default_user(settings.WEB_USERNAME, settings.WEB_PASSWORD)

    # ── Загрузка инструментов ─────────────────────────────────────────────────
    config = load_instruments_config()
    instruments = sync_instruments_to_db(config)

    # ── T-Invest: получаем account_id ─────────────────────────────────────────
    account_id = get_first_account_id()

    # ── Собираем компоненты (сейчас только SBER) ──────────────────────────────
    # Берём первый активный инструмент из конфига
    ticker = next(iter(instruments))
    params = instruments[ticker]

    strategy, order_manager, position_manager = build_components(ticker, params, account_id)
    recorder = DataRecorder(figi=params["figi"])
    on_orderbook, on_trade = make_event_handlers(strategy, position_manager, recorder)

    # ── Стрим рыночных данных ─────────────────────────────────────────────────
    stream = StreamHandler(
        figi=params["figi"],
        on_orderbook=on_orderbook,
        on_trade=on_trade,
        orderbook_depth=10,
        instrument_id=params.get("instrument_id", ""),
    )

    stream_thread = threading.Thread(target=stream.start, daemon=True, name="stream")
    stream_thread.start()
    logger.info(f"Стрим запущен для {ticker} ({params['figi']})")

    # ── Планировщик (проверка тайм-аута позиции) ──────────────────────────────
    scheduler = BackgroundScheduler()
    scheduler.add_job(position_manager.check_timeout, "interval", minutes=1, id="timeout_check")
    scheduler.start()
    logger.info("Планировщик запущен")

    # ── Веб-дашборд ───────────────────────────────────────────────────────────
    flask_app = create_app()
    set_position_manager(position_manager)

    web_thread = threading.Thread(target=run_web, args=(flask_app,), daemon=True, name="web")
    web_thread.start()
    logger.info(f"Веб-дашборд запущен: http://{settings.WEB_HOST}:{settings.WEB_PORT}")

    # ── Обработка Ctrl+C ──────────────────────────────────────────────────────
    def shutdown(signum, frame):
        logger.info("Получен сигнал остановки, завершение...")
        stream.stop()
        scheduler.shutdown(wait=False)
        repository.log_event("INFO", "main", "Бот остановлен")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    repository.log_event("INFO", "main", f"Бот запущен. Торгуем: {ticker}")
    logger.info(f"Бот запущен. Нажмите Ctrl+C для остановки.")

    # Главный поток ждёт — все рабочие потоки daemon
    stream_thread.join()


if __name__ == "__main__":
    main()
