"""
Глобальные настройки приложения.
Все секреты читаются из .env файла — никаких хардкодированных значений.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из корня проекта
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

# ─── T-Invest API ─────────────────────────────────────────────────────────────
TINKOFF_TOKEN: str = os.environ["TINKOFF_TOKEN"]
# Токен для чтения рыночных данных. Если не задан — используется TINKOFF_TOKEN.
TINKOFF_MARKET_TOKEN: str = os.environ.get("TINKOFF_MARKET_TOKEN") or TINKOFF_TOKEN

# ─── База данных ──────────────────────────────────────────────────────────────
MYSQL_URL: str = os.environ["MYSQL_URL"]

# ─── Веб-приложение ───────────────────────────────────────────────────────────
WEB_SECRET_KEY: str = os.environ["WEB_SECRET_KEY"]
WEB_USERNAME: str = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD: str = os.environ["WEB_PASSWORD"]
WEB_HOST: str = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.environ.get("WEB_PORT", "5000"))

# ─── Риск-менеджмент ──────────────────────────────────────────────────────────
# Дневной лимит убытков в рублях. При достижении бот прекращает торговлю до след. дня.
DAILY_LOSS_LIMIT_RUB: float = float(os.environ.get("DAILY_LOSS_LIMIT_RUB", "-500.0"))

# ─── Логирование ──────────────────────────────────────────────────────────────
LOG_DIR: Path = BASE_DIR / "logs"
LOG_FILE: Path = LOG_DIR / "bot.log"
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# ─── Инструменты ──────────────────────────────────────────────────────────────
INSTRUMENTS_CONFIG_PATH: Path = BASE_DIR / "trading_bot" / "config" / "instruments.yaml"

# ─── Режим работы ─────────────────────────────────────────────────────────────
# SANDBOX=true — использовать песочницу T-Invest (тестовые ордера)
USE_SANDBOX: bool = os.environ.get("SANDBOX", "false").lower() == "true"

# ─── Запись рыночных данных для бэктеста ──────────────────────────────────────
# RECORD_MARKET_DATA=true — писать снапшоты стакана и тики сделок в БД
# Внимание: SBER генерирует ~150-300k строк стакана в день
RECORD_MARKET_DATA: bool = os.environ.get("RECORD_MARKET_DATA", "false").lower() == "true"
# Записывать каждый N-й снапшот стакана (1 = каждый, 5 = каждый 5-й)
RECORD_ORDERBOOK_INTERVAL: int = int(os.environ.get("RECORD_ORDERBOOK_INTERVAL", "1"))
