"""
Аутентификация веб-интерфейса.
"""
import logging
from datetime import datetime
from typing import Optional

import bcrypt
from flask_login import UserMixin

from trading_bot.db import repository

logger = logging.getLogger(__name__)


class WebUser(UserMixin):
    """Flask-Login совместимый пользователь."""

    def __init__(self, user_id: int, username: str) -> None:
        self.id = user_id
        self.username = username

    def get_id(self) -> str:
        return str(self.id)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def load_user(user_id: str) -> Optional[WebUser]:
    """Flask-Login user_loader callback."""
    try:
        from trading_bot.db.repository import get_session
        from trading_bot.db.models import User
        with get_session() as session:
            user = session.get(User, int(user_id))
            if user and user.is_active:
                return WebUser(user.id, user.username)
    except Exception as e:
        logger.error(f"Ошибка загрузки пользователя {user_id}: {e}")
    return None


def authenticate(username: str, password: str) -> Optional[WebUser]:
    """Проверить логин и пароль. Вернуть WebUser при успехе или None."""
    user = repository.get_user_by_username(username)
    if user is None:
        return None
    if not check_password(password, user.password_hash):
        return None
    repository.update_last_login(user.id)
    return WebUser(user.id, user.username)


def ensure_default_user(username: str, password: str) -> None:
    """Создать пользователя по умолчанию если его нет."""
    existing = repository.get_user_by_username(username)
    if existing is None:
        hashed = hash_password(password)
        repository.create_user(username, hashed)
        logger.info(f"Создан пользователь по умолчанию: {username}")
