"""
Flask приложение — веб-дашборд торгового бота.
"""
import logging
from typing import Optional

from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from flask_login import LoginManager, login_required, login_user, logout_user

from trading_bot.config import settings
from trading_bot.web.auth import WebUser, authenticate, load_user as _load_user

logger = logging.getLogger(__name__)

# Менеджеры позиций по тикерам — инжектируются из main.py
# {ticker: PositionManager}
_position_managers: dict = {}
_portfolio_manager = None


def get_position_managers() -> dict:
    return _position_managers


def set_position_managers(pms: dict) -> None:
    global _position_managers
    _position_managers = pms


def get_portfolio_manager():
    return _portfolio_manager


def set_portfolio_manager(pm) -> None:
    global _portfolio_manager
    _portfolio_manager = pm


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = settings.WEB_SECRET_KEY

    # ── IP whitelist ──────────────────────────────────────────────────────────
    if settings.WEB_ALLOWED_IPS:
        @app.before_request
        def check_ip():
            remote_ip = request.remote_addr
            if remote_ip not in settings.WEB_ALLOWED_IPS:
                logger.warning("Blocked request from %s (not in WEB_ALLOWED_IPS)", remote_ip)
                abort(403)

    # ── Flask-Login ────────────────────────────────────────────────────────────
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Необходима авторизация"

    @login_manager.user_loader
    def user_loader(user_id: str):
        return _load_user(user_id)

    # ── Auth routes ───────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            user = authenticate(username, password)
            if user:
                login_user(user)
                next_url = request.args.get("next", "")
                if not next_url or urlparse(next_url).netloc:
                    next_url = url_for("dashboard.index")
                return redirect(next_url)
            error = "Неверный логин или пароль"
        return render_template("login.html", error=error)

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ── Blueprints ─────────────────────────────────────────────────────────────
    from trading_bot.web.routes.dashboard import bp as dashboard_bp
    from trading_bot.web.routes.trades import bp as trades_bp
    from trading_bot.web.routes.signals import bp_signals, bp_stats
    from trading_bot.web.routes.instruments import bp as instruments_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(trades_bp)
    app.register_blueprint(bp_signals)
    app.register_blueprint(bp_stats)
    app.register_blueprint(instruments_bp)

    # ── Error handlers ─────────────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="Доступ запрещён"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="Страница не найдена"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("error.html", code=500, message="Внутренняя ошибка сервера"), 500

    return app
