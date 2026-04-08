"""
Слой доступа к данным.
Все операции с БД централизованы здесь — стратегии и бизнес-логика
не работают с сессиями напрямую.
"""
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Generator, List, Optional

from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import Session, sessionmaker

from trading_bot.config import settings
from trading_bot.db.models import (
    Base, BotLog, BotState, Instrument, MarketOrderbook, MarketTradeTick,
    Order, Signal, Trade, User,
)

logger = logging.getLogger(__name__)


def create_db_engine():
    return create_engine(
        settings.MYSQL_URL,
        pool_pre_ping=True,      # автопроверка соединения перед использованием
        pool_recycle=3600,       # пересоздавать соединение каждый час
        pool_size=5,
        max_overflow=10,
    )


engine = create_db_engine()
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Создать все таблицы если их нет. Вызывается при старте."""
    Base.metadata.create_all(engine)
    # Инициализировать singleton BotState если его нет
    with get_session() as session:
        state = session.get(BotState, 1)
        if state is None:
            session.add(BotState(id=1, bot_active=False))
            session.commit()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Контекстный менеджер сессии с автоматическим rollback при ошибке."""
    session = SessionFactory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─── Instruments ──────────────────────────────────────────────────────────────

def get_instrument_by_ticker(ticker: str) -> Optional[Instrument]:
    with get_session() as session:
        return session.query(Instrument).filter_by(ticker=ticker).first()


def get_instrument_by_figi(figi: str) -> Optional[Instrument]:
    with get_session() as session:
        return session.query(Instrument).filter_by(figi=figi).first()


def get_active_instruments() -> List[Instrument]:
    with get_session() as session:
        return session.query(Instrument).filter_by(is_active=True).all()


def upsert_instrument(data: dict) -> Instrument:
    """Создать или обновить инструмент по тикеру."""
    with get_session() as session:
        inst = session.query(Instrument).filter_by(ticker=data["ticker"]).first()
        if inst is None:
            inst = Instrument(**data)
            session.add(inst)
        else:
            for key, value in data.items():
                setattr(inst, key, value)
            inst.updated_at = datetime.utcnow()
        session.commit()
        return inst


# ─── Signals ──────────────────────────────────────────────────────────────────

def save_signal(
    instrument_id: int,
    signal_type: str,
    ofi_value: float,
    print_volume: Optional[float],
    print_side: Optional[str],
    reason: str,
    acted_on: bool = False,
) -> Signal:
    with get_session() as session:
        sig = Signal(
            instrument_id=instrument_id,
            signal_type=signal_type,
            ofi_value=ofi_value,
            print_volume=print_volume,
            print_side=print_side,
            reason=reason,
            acted_on=acted_on,
        )
        session.add(sig)
        session.commit()
        return sig


def mark_signal_acted(signal_id: int) -> None:
    with get_session() as session:
        sig = session.get(Signal, signal_id)
        if sig:
            sig.acted_on = True
            session.commit()


def get_recent_signals(limit: int = 50) -> List[Signal]:
    with get_session() as session:
        return (
            session.query(Signal)
            .order_by(Signal.created_at.desc())
            .limit(limit)
            .all()
        )


def get_signals_page(page: int = 1, per_page: int = 50) -> tuple[List[Signal], int]:
    with get_session() as session:
        total = session.query(func.count(Signal.id)).scalar()
        signals = (
            session.query(Signal)
            .order_by(Signal.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return signals, total


# ─── Orders ───────────────────────────────────────────────────────────────────

def save_order(
    instrument_id: int,
    signal_id: Optional[int],
    direction: str,
    quantity: int,
    order_id_broker: Optional[str] = None,
    price_requested: Optional[float] = None,
) -> Order:
    with get_session() as session:
        order = Order(
            instrument_id=instrument_id,
            signal_id=signal_id,
            direction=direction,
            quantity=quantity,
            order_id_broker=order_id_broker,
            price_requested=price_requested,
            status="new",
        )
        session.add(order)
        session.commit()
        return order


def update_order_status(
    order_id: int,
    status: str,
    price_executed: Optional[float] = None,
    commission_rub: Optional[float] = None,
    order_id_broker: Optional[str] = None,
) -> None:
    with get_session() as session:
        order = session.get(Order, order_id)
        if order:
            order.status = status
            if price_executed is not None:
                order.price_executed = price_executed
            if commission_rub is not None:
                order.commission_rub = commission_rub
            if order_id_broker is not None:
                order.order_id_broker = order_id_broker
            order.updated_at = datetime.utcnow()
            session.commit()


def get_order_by_broker_id(broker_id: str) -> Optional[Order]:
    with get_session() as session:
        return session.query(Order).filter_by(order_id_broker=broker_id).first()


# ─── Trades ───────────────────────────────────────────────────────────────────

def save_trade(
    instrument_id: int,
    direction: str,
    open_price: float,
    close_price: float,
    quantity: int,
    pnl_rub: float,
    commission_rub: float,
    open_at: datetime,
    close_at: datetime,
    exit_reason: str,
    open_order_id: Optional[int] = None,
    close_order_id: Optional[int] = None,
) -> Trade:
    hold_seconds = int((close_at - open_at).total_seconds())
    with get_session() as session:
        trade = Trade(
            instrument_id=instrument_id,
            direction=direction,
            open_price=open_price,
            close_price=close_price,
            quantity=quantity,
            pnl_rub=pnl_rub,
            commission_rub=commission_rub,
            open_at=open_at,
            close_at=close_at,
            hold_seconds=hold_seconds,
            exit_reason=exit_reason,
            open_order_id=open_order_id,
            close_order_id=close_order_id,
        )
        session.add(trade)
        session.commit()
        return trade


def get_today_pnl(instrument_id: Optional[int] = None) -> float:
    """Суммарный P&L за сегодня (в рублях)."""
    today = date.today()
    with get_session() as session:
        q = session.query(func.coalesce(func.sum(Trade.pnl_rub), 0.0)).filter(
            func.date(Trade.close_at) == today
        )
        if instrument_id is not None:
            q = q.filter(Trade.instrument_id == instrument_id)
        return float(q.scalar())


def get_trades_page(
    page: int = 1,
    per_page: int = 50,
    direction: Optional[str] = None,
    exit_reason: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> tuple[List[Trade], int]:
    with get_session() as session:
        q = session.query(Trade)
        if direction:
            q = q.filter(Trade.direction == direction)
        if exit_reason:
            q = q.filter(Trade.exit_reason == exit_reason)
        if date_from:
            q = q.filter(Trade.open_at >= date_from)
        if date_to:
            q = q.filter(Trade.open_at < date_to + timedelta(days=1))
        total = q.count()
        trades = q.order_by(Trade.close_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        return trades, total


def get_all_trades_for_export(
    direction: Optional[str] = None,
    exit_reason: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> List[Trade]:
    with get_session() as session:
        q = session.query(Trade)
        if direction:
            q = q.filter(Trade.direction == direction)
        if exit_reason:
            q = q.filter(Trade.exit_reason == exit_reason)
        if date_from:
            q = q.filter(Trade.open_at >= date_from)
        if date_to:
            q = q.filter(Trade.open_at < date_to + timedelta(days=1))
        return q.order_by(Trade.close_at.desc()).all()


def get_stats_summary() -> dict:
    """Агрегированная статистика по всем сделкам."""
    with get_session() as session:
        total_trades = session.query(func.count(Trade.id)).scalar() or 0
        if total_trades == 0:
            return {
                "total_trades": 0, "win_rate": 0, "total_pnl": 0,
                "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
                "best_trade": 0, "worst_trade": 0, "avg_hold_seconds": 0,
            }

        total_pnl = float(session.query(func.sum(Trade.pnl_rub)).scalar() or 0)
        wins = session.query(func.count(Trade.id)).filter(Trade.pnl_rub > 0).scalar() or 0
        losses = session.query(func.count(Trade.id)).filter(Trade.pnl_rub <= 0).scalar() or 0

        avg_win = float(
            session.query(func.avg(Trade.pnl_rub)).filter(Trade.pnl_rub > 0).scalar() or 0
        )
        avg_loss = float(
            session.query(func.avg(Trade.pnl_rub)).filter(Trade.pnl_rub <= 0).scalar() or 0
        )
        best = float(session.query(func.max(Trade.pnl_rub)).scalar() or 0)
        worst = float(session.query(func.min(Trade.pnl_rub)).scalar() or 0)
        avg_hold = float(session.query(func.avg(Trade.hold_seconds)).scalar() or 0)

        gross_profit = float(
            session.query(func.sum(Trade.pnl_rub)).filter(Trade.pnl_rub > 0).scalar() or 0
        )
        gross_loss = abs(float(
            session.query(func.sum(Trade.pnl_rub)).filter(Trade.pnl_rub <= 0).scalar() or 0
        ))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return {
            "total_trades": total_trades,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "best_trade": round(best, 2),
            "worst_trade": round(worst, 2),
            "avg_hold_seconds": int(avg_hold),
        }


def get_pnl_by_day(days: int = 30) -> List[dict]:
    """P&L по дням за последние N дней."""
    with get_session() as session:
        rows = (
            session.query(
                func.date(Trade.close_at).label("day"),
                func.sum(Trade.pnl_rub).label("pnl"),
            )
            .filter(Trade.close_at >= datetime.utcnow() - timedelta(days=days))
            .group_by(func.date(Trade.close_at))
            .order_by(func.date(Trade.close_at))
            .all()
        )
        return [{"day": str(r.day), "pnl": round(float(r.pnl), 2)} for r in rows]


def get_pnl_by_hour() -> List[dict]:
    """P&L по часу дня (0-23) — для анализа торговых паттернов."""
    with get_session() as session:
        rows = (
            session.query(
                func.hour(Trade.close_at).label("hour"),
                func.sum(Trade.pnl_rub).label("pnl"),
                func.count(Trade.id).label("count"),
            )
            .group_by(func.hour(Trade.close_at))
            .order_by(func.hour(Trade.close_at))
            .all()
        )
        return [{"hour": r.hour, "pnl": round(float(r.pnl), 2), "count": r.count} for r in rows]


def get_pnl_by_weekday() -> List[dict]:
    """P&L по дню недели (1=Monday...7=Sunday)."""
    with get_session() as session:
        rows = (
            session.query(
                func.dayofweek(Trade.close_at).label("dow"),
                func.sum(Trade.pnl_rub).label("pnl"),
                func.count(Trade.id).label("count"),
            )
            .group_by(func.dayofweek(Trade.close_at))
            .order_by(func.dayofweek(Trade.close_at))
            .all()
        )
        days_map = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}
        return [
            {"dow": days_map.get(r.dow, str(r.dow)), "pnl": round(float(r.pnl), 2), "count": r.count}
            for r in rows
        ]


# ─── BotLogs ──────────────────────────────────────────────────────────────────

def log_event(level: str, component: str, message: str) -> None:
    """Записать системное событие в БД. Не бросает исключений."""
    try:
        with get_session() as session:
            entry = BotLog(level=level, component=component, message=message)
            session.add(entry)
            session.commit()
    except Exception as e:
        logger.error(f"Не удалось записать log в БД: {e}")


def get_recent_logs(limit: int = 100) -> List[BotLog]:
    with get_session() as session:
        return (
            session.query(BotLog)
            .order_by(BotLog.created_at.desc())
            .limit(limit)
            .all()
        )


# ─── BotState ─────────────────────────────────────────────────────────────────

def get_bot_active() -> bool:
    with get_session() as session:
        state = session.get(BotState, 1)
        return bool(state and state.bot_active)


def set_bot_active(value: bool) -> None:
    with get_session() as session:
        state = session.get(BotState, 1)
        if state is None:
            state = BotState(id=1, bot_active=value)
            session.add(state)
        else:
            state.bot_active = value
            state.updated_at = datetime.utcnow()
        session.commit()


# ─── Users ────────────────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> Optional[User]:
    with get_session() as session:
        return session.query(User).filter_by(username=username).first()


def create_user(username: str, password_hash: str) -> User:
    with get_session() as session:
        user = User(username=username, password_hash=password_hash)
        session.add(user)
        session.commit()
        return user


def update_last_login(user_id: int) -> None:
    with get_session() as session:
        user = session.get(User, user_id)
        if user:
            user.last_login = datetime.utcnow()
            session.commit()


# ─── Market data (backtest recording) ────────────────────────────────────────

def save_orderbook_snapshot(
    figi: str,
    bids: list,
    asks: list,
    timestamp: datetime,
) -> None:
    import json
    with get_session() as session:
        row = MarketOrderbook(
            figi=figi,
            bids=json.dumps(bids),
            asks=json.dumps(asks),
            recorded_at=timestamp,
        )
        session.add(row)
        session.commit()


def save_trade_tick(
    figi: str,
    price: float,
    quantity: int,
    direction: str,
    timestamp: datetime,
) -> None:
    with get_session() as session:
        row = MarketTradeTick(
            figi=figi,
            price=price,
            quantity=quantity,
            direction=direction,
            recorded_at=timestamp,
        )
        session.add(row)
        session.commit()


def get_orderbook_snapshots(
    figi: str,
    date_from: datetime,
    date_to: datetime,
) -> List[MarketOrderbook]:
    with get_session() as session:
        return (
            session.query(MarketOrderbook)
            .filter(
                MarketOrderbook.figi == figi,
                MarketOrderbook.recorded_at >= date_from,
                MarketOrderbook.recorded_at < date_to,
            )
            .order_by(MarketOrderbook.recorded_at)
            .all()
        )


def iter_orderbook_snapshots(
    figi: str,
    date_from: datetime,
    date_to: datetime,
    chunk_size: int = 2000,
) -> Generator[tuple, None, None]:
    """Потоковое чтение стакана — unbuffered cursor, не грузит всё в RAM."""
    with get_session() as session:
        q = (
            session.query(
                MarketOrderbook.recorded_at,
                MarketOrderbook.bids,
                MarketOrderbook.asks,
            )
            .filter(
                MarketOrderbook.figi == figi,
                MarketOrderbook.recorded_at >= date_from,
                MarketOrderbook.recorded_at < date_to,
            )
            .order_by(MarketOrderbook.recorded_at)
            .execution_options(stream_results=True)
            .yield_per(chunk_size)
        )
        for row in q:
            yield row.recorded_at, row.bids, row.asks


def get_trade_ticks(
    figi: str,
    date_from: datetime,
    date_to: datetime,
) -> List[MarketTradeTick]:
    with get_session() as session:
        return (
            session.query(MarketTradeTick)
            .filter(
                MarketTradeTick.figi == figi,
                MarketTradeTick.recorded_at >= date_from,
                MarketTradeTick.recorded_at < date_to,
            )
            .order_by(MarketTradeTick.recorded_at)
            .all()
        )


def iter_trade_ticks(
    figi: str,
    date_from: datetime,
    date_to: datetime,
    chunk_size: int = 5000,
) -> Generator[tuple, None, None]:
    """Потоковое чтение сделок — unbuffered cursor, не грузит всё в RAM."""
    with get_session() as session:
        q = (
            session.query(
                MarketTradeTick.recorded_at,
                MarketTradeTick.price,
                MarketTradeTick.quantity,
                MarketTradeTick.direction,
            )
            .filter(
                MarketTradeTick.figi == figi,
                MarketTradeTick.recorded_at >= date_from,
                MarketTradeTick.recorded_at < date_to,
            )
            .order_by(MarketTradeTick.recorded_at)
            .execution_options(stream_results=True)
            .yield_per(chunk_size)
        )
        for row in q:
            yield row.recorded_at, row.price, row.quantity, row.direction


def get_recorded_dates(figi: str) -> List[str]:
    """Список дат (UTC), для которых есть данные стакана."""
    with get_session() as session:
        rows = (
            session.query(func.date(MarketOrderbook.recorded_at).label("d"))
            .filter(MarketOrderbook.figi == figi)
            .group_by(func.date(MarketOrderbook.recorded_at))
            .order_by(func.date(MarketOrderbook.recorded_at))
            .all()
        )
        return [str(r.d) for r in rows]
