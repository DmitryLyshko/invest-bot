"""
Модели SQLAlchemy для всех таблиц торгового бота.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Настройки торгового инструмента. Каждая строка — один тикер."""
    __tablename__ = "instruments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, unique=True)
    figi = Column(String(50), nullable=False, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Параметры стратегии — синхронизируются из instruments.yaml при старте
    ofi_threshold = Column(Float, default=0.6, nullable=False)
    print_multiplier = Column(Float, default=7.0, nullable=False)
    print_window = Column(Integer, default=200, nullable=False)
    ofi_levels = Column(Integer, default=5, nullable=False)
    cooldown_seconds = Column(Integer, default=60, nullable=False)
    max_hold_minutes = Column(Integer, default=60, nullable=False)
    stop_ticks = Column(Integer, default=3, nullable=False)
    lot_size = Column(Integer, default=1, nullable=False)

    # Параметры стабилизации выхода (добавлены в FIX_01)
    ofi_smooth_window = Column(Integer, default=10, nullable=False)
    min_hold_seconds = Column(Integer, default=30, nullable=False)
    ofi_exit_threshold = Column(Float, default=0.4, nullable=False)
    min_ofi_confirmations = Column(Integer, default=3, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    signals = relationship("Signal", back_populates="instrument")
    orders = relationship("Order", back_populates="instrument")
    trades = relationship("Trade", back_populates="instrument")

    def __repr__(self) -> str:
        return f"<Instrument {self.ticker}>"


class Signal(Base):
    """Все сгенерированные сигналы стратегии, включая те, по которым ордер не выставлялся."""
    __tablename__ = "signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)

    # Тип сигнала: long / short / exit
    signal_type = Column(String(10), nullable=False)

    # Значения индикаторов в момент сигнала
    ofi_value = Column(Float, nullable=True)       # от -1 до 1
    print_volume = Column(Float, nullable=True)    # объём крупного принта
    print_side = Column(String(10), nullable=True) # buy / sell

    # Причина генерации сигнала
    # combo_triggered — оба индикатора совпали (вход)
    # ofi_reversed    — OFI развернулся против позиции (выход)
    # timeout         — истёк max_hold_minutes (выход)
    # stop_loss       — сработал стоп-лосс (выход)
    reason = Column(String(30), nullable=True)

    # Была ли выставлен ордер на основе этого сигнала
    acted_on = Column(Boolean, default=False, nullable=False)

    # Название стратегии: 'combo' (OFI+Print) или 'rsi' (Augmented RSI 5m)
    strategy_name = Column(String(50), default="combo", nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    instrument = relationship("Instrument", back_populates="signals")
    orders = relationship("Order", back_populates="signal")

    def __repr__(self) -> str:
        return f"<Signal {self.signal_type} ofi={self.ofi_value:.3f}>"


class Order(Base):
    """Все выставленные ордера через T-Invest API."""
    __tablename__ = "orders"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    signal_id = Column(BigInteger, ForeignKey("signals.id"), nullable=True)

    # Идентификатор ордера на стороне брокера
    order_id_broker = Column(String(100), nullable=True, unique=True)

    direction = Column(String(10), nullable=False)   # buy / sell
    quantity = Column(Integer, nullable=False)        # в лотах
    price_requested = Column(Float, nullable=True)    # None для рыночных ордеров
    price_executed = Column(Float, nullable=True)     # заполняется при исполнении
    commission_rub = Column(Float, nullable=True)

    # new / pending / filled / cancelled / rejected
    status = Column(String(20), default="new", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    instrument = relationship("Instrument", back_populates="orders")
    signal = relationship("Signal", back_populates="orders")

    def __repr__(self) -> str:
        return f"<Order {self.direction} {self.quantity}л status={self.status}>"


class Trade(Base):
    """Завершённые сделки: каждая строка — полный цикл вход+выход."""
    __tablename__ = "trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)

    open_order_id = Column(BigInteger, ForeignKey("orders.id"), nullable=True)
    close_order_id = Column(BigInteger, ForeignKey("orders.id"), nullable=True)

    direction = Column(String(10), nullable=False)  # long / short

    open_price = Column(Float, nullable=False)
    close_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)  # в лотах

    pnl_rub = Column(Float, nullable=False)
    commission_rub = Column(Float, nullable=False, default=0.0)

    open_at = Column(DateTime, nullable=False)
    close_at = Column(DateTime, nullable=False)
    hold_seconds = Column(Integer, nullable=False)

    # ofi_reversed / manual / risk_limit / timeout / stop_loss
    exit_reason = Column(String(30), nullable=False)

    # Название стратегии: 'combo' (OFI+Print) или 'rsi' (Augmented RSI 5m)
    strategy_name = Column(String(50), default="combo", nullable=True)

    instrument = relationship("Instrument", back_populates="trades")
    open_order = relationship("Order", foreign_keys=[open_order_id])
    close_order = relationship("Order", foreign_keys=[close_order_id])

    def __repr__(self) -> str:
        return f"<Trade {self.direction} pnl={self.pnl_rub:.2f}р>"


class BotLog(Base):
    """Системные события, ошибки, отказы риск-менеджера."""
    __tablename__ = "bot_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    level = Column(String(10), nullable=False)      # INFO / WARNING / ERROR
    component = Column(String(50), nullable=False)  # risk_manager / order_manager / etc.
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<BotLog [{self.level}] {self.component}: {self.message[:50]}>"


class User(Base):
    """Пользователи веб-интерфейса."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_login = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class BotState(Base):
    """Глобальное состояние бота — singleton-запись."""
    __tablename__ = "bot_state"

    id = Column(Integer, primary_key=True, default=1)
    bot_active = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StrategyState(Base):
    """Состояние включения/выключения каждой стратегии."""
    __tablename__ = "strategy_state"

    strategy_name = Column(String(50), primary_key=True)
    is_active = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MarketOrderbook(Base):
    """Снапшоты стакана для бэктеста. Запись включается через RECORD_MARKET_DATA=true."""
    __tablename__ = "market_orderbooks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    figi = Column(String(50), nullable=False)
    bids = Column(Text, nullable=False)  # JSON: [[price, qty], ...]
    asks = Column(Text, nullable=False)  # JSON: [[price, qty], ...]
    recorded_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_market_orderbooks_figi_time", "figi", "recorded_at"),
    )


class MarketTradeTick(Base):
    """Тиковые сделки для бэктеста. Запись включается через RECORD_MARKET_DATA=true."""
    __tablename__ = "market_trade_ticks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    figi = Column(String(50), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    direction = Column(String(10), nullable=False)
    recorded_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_market_trade_ticks_figi_time", "figi", "recorded_at"),
    )
