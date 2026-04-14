"""
Миграция: добавить колонку strategy_name в signals/trades
и создать таблицу strategy_state.
Запустить один раз: python migrate_add_strategy_name.py
"""
import sys
from sqlalchemy import text

sys.path.insert(0, ".")
from trading_bot.db.repository import engine
from trading_bot.db.models import Base


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() "
        "AND table_name = :table AND column_name = :column"
    ), {"table": table, "column": column})
    return result.scalar() > 0


def main():
    # Создать новые таблицы (strategy_state)
    Base.metadata.create_all(engine)
    print("Таблицы созданы (если не существовали)")

    with engine.connect() as conn:
        for table in ("signals", "trades"):
            if column_exists(conn, table, "strategy_name"):
                print(f"{table}.strategy_name — уже существует, пропускаем")
            else:
                conn.execute(text(
                    f"ALTER TABLE `{table}` "
                    "ADD COLUMN strategy_name VARCHAR(50) NULL DEFAULT 'combo'"
                ))
                conn.commit()
                print(f"{table}.strategy_name — добавлена")

    # Инициализировать записи strategy_state
    from trading_bot.db.repository import init_strategy_states
    init_strategy_states()
    print("strategy_state инициализирована")

    print("Миграция завершена.")


if __name__ == "__main__":
    main()
