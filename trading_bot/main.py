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
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from tinkoff.invest import CandleInterval, Client
from tinkoff.invest.sandbox.client import SandboxClient
from tinkoff.invest.utils import quotation_to_decimal

from trading_bot.config import settings
from trading_bot.core.data.data_recorder import DataRecorder
from trading_bot.core.data.stream_handler import StreamHandler
from trading_bot.core.execution.order_manager import OrderManager
from trading_bot.core.execution.portfolio_manager import PortfolioManager
from trading_bot.core.execution.position_manager import PositionManager
from trading_bot.core.strategy.combo_strategy import ComboStrategy
from trading_bot.core.strategy.rsi_strategy import RSIStrategy
from trading_bot.db import repository
from trading_bot.db.clickhouse import init_clickhouse
from trading_bot.notifications.telegram_notifier import TelegramNotifier
from trading_bot.web.app import create_app, set_portfolio_manager, set_position_managers
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


RSI_CONFIG_PATH = settings.INSTRUMENTS_CONFIG_PATH.parent / "rsi_config.yaml"


def load_instruments_config() -> dict:
    """Загрузить конфигурацию инструментов из YAML."""
    with open(settings.INSTRUMENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rsi_config() -> dict:
    """Загрузить конфигурацию RSI-стратегии из YAML. Возвращает {} если файл не найден."""
    if not RSI_CONFIG_PATH.exists():
        return {}
    with open(RSI_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


def build_components(
    ticker: str,
    instrument_params: dict,
    account_id: str,
    portfolio_manager: PortfolioManager,
    notifier: TelegramNotifier,
):
    """Создать все торговые компоненты для одного инструмента (стратегия Combo)."""
    instrument_id = instrument_params["db_instrument_id"]

    strategy = ComboStrategy(instrument_params)
    order_manager = OrderManager(account_id, instrument_id)
    position_manager = PositionManager(
        instrument_id=instrument_id,
        instrument_config=instrument_params,
        order_manager=order_manager,
        strategy=strategy,
        portfolio_manager=portfolio_manager,
        ticker=ticker,
        notifier=notifier,
        strategy_name="combo",
    )

    return strategy, order_manager, position_manager


def build_rsi_components(
    ticker: str,
    instrument_params: dict,
    rsi_params: dict,
    account_id: str,
    portfolio_manager: PortfolioManager,
    notifier: TelegramNotifier,
):
    """
    Создать компоненты RSI-стратегии для одного инструмента.

    instrument_params — данные из instruments.yaml (figi, instrument_id, lot_size, tick_size, …)
    rsi_params        — данные из rsi_config.yaml (алго-параметры + риск-параметры RSI)
    """
    instrument_id = instrument_params["db_instrument_id"]

    # Мержим: базовые параметры инструмента + RSI-специфичные параметры
    merged = {
        "figi": instrument_params["figi"],
        "instrument_id": instrument_params.get("instrument_id", ""),
        "lot_size": instrument_params["lot_size"],
        "tick_size": instrument_params.get("tick_size", 0.01),
        "commission_rate": instrument_params.get("commission_rate", 0.0004),
        **rsi_params,
    }

    strategy = RSIStrategy(merged)
    order_manager = OrderManager(account_id, instrument_id)
    position_manager = PositionManager(
        instrument_id=instrument_id,
        instrument_config=merged,
        order_manager=order_manager,
        strategy=strategy,
        portfolio_manager=portfolio_manager,
        ticker=ticker,
        notifier=notifier,
        strategy_name="rsi",
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


def refresh_rsi_atr(figi: str, strategy: "RSIStrategy", rsi_params: dict) -> None:
    """
    Загрузить 5-минутные свечи из T-Invest API и обновить ATR в стратегии.
    Вызывается из планировщика каждые 5 минут.

    short_atr — средний ATR последних atr_length_short свечей (текущая активность).
    long_atr  — средний ATR за последние atr_days торговых дней (историческая норма).
    """
    logger = logging.getLogger(__name__)
    atr_ratio_min = rsi_params.get("atr_ratio_min", 0.0)
    if atr_ratio_min <= 0:
        return  # фильтр отключён

    atr_short_len = rsi_params.get("atr_length_short", 5)
    atr_days = rsi_params.get("atr_days", 5)

    # Берём calendar_days с запасом на выходные: 5 торговых дней ≈ 7-8 календарных
    calendar_days = atr_days + 3
    now = datetime.now(timezone.utc)
    from_ = now - timedelta(days=calendar_days)

    try:
        ClientClass = SandboxClient if settings.USE_SANDBOX else Client
        with ClientClass(settings.TINKOFF_TOKEN) as client:
            resp = client.market_data.get_candles(
                figi=figi,
                from_=from_,
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
            )
        candles = [c for c in resp.candles if c.is_complete]
        if len(candles) < atr_short_len:
            logger.debug(f"ATR refresh {figi}: недостаточно свечей ({len(candles)})")
            return

        trs = [
            float(quotation_to_decimal(c.high)) - float(quotation_to_decimal(c.low))
            for c in candles
        ]

        short_atr = sum(trs[-atr_short_len:]) / atr_short_len
        # Базовая норма = среднее по всем свечам за N дней
        long_atr = sum(trs) / len(trs)

        logger.debug(
            f"ATR {figi}: short={short_atr:.4f}, "
            f"5d_avg={long_atr:.4f}, "
            f"ratio={short_atr/long_atr:.2f} (min={atr_ratio_min})"
            if long_atr > 0 else f"ATR {figi}: long_atr=0"
        )
        strategy.update_atr(short_atr, long_atr)

    except Exception:
        logger.exception(f"Ошибка при обновлении ATR для {figi}")


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
    init_clickhouse()   # no-op если CLICKHOUSE_HOST не задан

    # ── Пользователь по умолчанию ─────────────────────────────────────────────
    ensure_default_user(settings.WEB_USERNAME, settings.WEB_PASSWORD)

    # ── Загрузка инструментов ─────────────────────────────────────────────────
    config = load_instruments_config()
    instruments = sync_instruments_to_db(config)
    rsi_config = load_rsi_config()
    logger.info(f"RSI-конфиг загружен для тикеров: {list(rsi_config.keys()) or '[]'}")

    # ── T-Invest: получаем account_id ─────────────────────────────────────────
    account_id = get_first_account_id()

    # ── Telegram-уведомления ──────────────────────────────────────────────────
    notifier = TelegramNotifier(
        token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
    )

    # ── Менеджер портфеля (общий для всех тикеров) ────────────────────────────
    all_figis = [params["figi"] for params in instruments.values()]
    portfolio_manager = PortfolioManager(
        account_id=account_id,
        max_positions=settings.MAX_GLOBAL_POSITIONS,
        max_position_pct=settings.MAX_POSITION_PCT,
        figis=all_figis,
    )
    portfolio_manager.refresh()
    logger.info(
        f"Портфель: {portfolio_manager.portfolio_value:.2f} руб. "
        f"(лимит {settings.MAX_GLOBAL_POSITIONS} позиций, "
        f"макс {settings.MAX_POSITION_PCT*100:.0f}% на сделку)"
    )

    # ── Планировщик (проверка тайм-аутов позиций) ─────────────────────────────
    scheduler = BackgroundScheduler()

    # ── Собираем компоненты для каждого инструмента ───────────────────────────
    position_managers: Dict[str, PositionManager] = {}
    all_streams = []

    for ticker, params in instruments.items():
        strategy, order_manager, position_manager = build_components(
            ticker, params, account_id, portfolio_manager, notifier
        )
        recorder = DataRecorder(figi=params["figi"], instrument_config=params)
        on_orderbook, on_trade = make_event_handlers(strategy, position_manager, recorder)

        stream = StreamHandler(
            figi=params["figi"],
            on_orderbook=on_orderbook,
            on_trade=on_trade,
            orderbook_depth=10,
            instrument_id=params.get("instrument_id", ""),
        )

        t = threading.Thread(target=stream.start, daemon=True, name=f"stream_{ticker}")
        t.start()
        logger.info(f"Стрим запущен для {ticker} ({params['figi']})")

        scheduler.add_job(
            position_manager.check_timeout,
            "interval",
            minutes=1,
            id=f"timeout_check_{ticker}",
        )

        position_manager.recover_position(params["figi"])
        position_managers[ticker] = position_manager
        all_streams.append(stream)

    # ── RSI-стратегия (5-минутные свечи) ─────────────────────────────────────
    for ticker, rsi_params in rsi_config.items():
        if ticker not in instruments:
            logger.warning(f"RSI: тикер {ticker} из rsi_config.yaml не найден в instruments.yaml, пропускаем")
            continue

        params = instruments[ticker]
        rsi_key = f"rsi_{ticker}"

        rsi_strategy, rsi_order_manager, rsi_position_manager = build_rsi_components(
            ticker=ticker,
            instrument_params=params,
            rsi_params=rsi_params,
            account_id=account_id,
            portfolio_manager=portfolio_manager,
            notifier=notifier,
        )
        recorder_rsi = DataRecorder(figi=params["figi"], instrument_config=params)
        _, on_trade_rsi = make_event_handlers(rsi_strategy, rsi_position_manager, recorder_rsi)

        # RSI использует только on_trade (candle aggregator), on_orderbook — no-op
        def _make_rsi_on_orderbook(strat, pm, rec):
            def on_ob(data):
                rec.on_orderbook(data)
                strat.on_orderbook(data)
            return on_ob

        on_orderbook_rsi = _make_rsi_on_orderbook(rsi_strategy, rsi_position_manager, recorder_rsi)

        rsi_stream = StreamHandler(
            figi=params["figi"],
            on_orderbook=on_orderbook_rsi,
            on_trade=on_trade_rsi,
            orderbook_depth=10,
            instrument_id=params.get("instrument_id", ""),
        )

        t_rsi = threading.Thread(
            target=rsi_stream.start, daemon=True, name=f"rsi_stream_{ticker}"
        )
        t_rsi.start()
        logger.info(f"RSI стрим запущен для {ticker}")

        scheduler.add_job(
            rsi_position_manager.check_timeout,
            "interval",
            minutes=1,
            id=f"rsi_timeout_check_{ticker}",
        )
        scheduler.add_job(
            rsi_position_manager.check_eod_close,
            "interval",
            minutes=1,
            id=f"rsi_eod_close_{ticker}",
        )

        # ATR-фильтр: обновлять каждые 5 минут
        if rsi_params.get("atr_ratio_min", 0) > 0:
            scheduler.add_job(
                refresh_rsi_atr,
                "interval",
                minutes=5,
                id=f"rsi_atr_refresh_{ticker}",
                args=[params["figi"], rsi_strategy, rsi_params],
            )
            # Сразу загружаем при старте
            refresh_rsi_atr(params["figi"], rsi_strategy, rsi_params)

        rsi_position_manager.recover_position(params["figi"])
        position_managers[rsi_key] = rsi_position_manager
        all_streams.append(rsi_stream)

    # Обновлять стоимость портфеля каждую минуту
    scheduler.add_job(portfolio_manager.refresh, "interval", minutes=1, id="portfolio_refresh")

    # Уведомление о начале торговой сессии — 10:05 МСК (07:05 UTC), по будням
    all_tickers = list(instruments.keys())
    scheduler.add_job(
        notifier.send_trading_day_started,
        "cron",
        day_of_week="mon-fri",
        hour=7,
        minute=5,
        kwargs={"tickers": all_tickers},
        id="trading_day_start_notify",
        timezone="UTC",
    )

    scheduler.start()
    logger.info(f"Планировщик запущен для {len(instruments)} инструментов")

    # ── Веб-дашборд ───────────────────────────────────────────────────────────
    flask_app = create_app()
    set_position_managers(position_managers)
    set_portfolio_manager(portfolio_manager)

    web_thread = threading.Thread(target=run_web, args=(flask_app,), daemon=True, name="web")
    web_thread.start()
    logger.info(f"Веб-дашборд запущен: http://{settings.WEB_HOST}:{settings.WEB_PORT}")

    # ── Обработка Ctrl+C ──────────────────────────────────────────────────────
    stop_event = threading.Event()

    def shutdown(signum, frame):
        logger.info("Получен сигнал остановки, завершение...")
        for s in all_streams:
            s.stop()
        scheduler.shutdown(wait=False)
        repository.log_event("INFO", "main", "Бот остановлен")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    tickers_str = ", ".join(instruments.keys())
    repository.log_event("INFO", "main", f"Бот запущен. Торгуем: {tickers_str}")
    logger.info(f"Бот запущен. Тикеры: {tickers_str}. Нажмите Ctrl+C для остановки.")

    notifier.send_bot_started(tickers=list(instruments.keys()), sandbox=settings.USE_SANDBOX)

    # Главный поток ждёт сигнала остановки
    stop_event.wait()


if __name__ == "__main__":
    main()
