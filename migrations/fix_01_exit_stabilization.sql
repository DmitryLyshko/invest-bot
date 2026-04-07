-- FIX_01: Добавление полей стабилизации выхода по OFI
-- Применять: sqlite3 trading_bot.db < migrations/fix_01_exit_stabilization.sql

ALTER TABLE instruments ADD COLUMN ofi_smooth_window    INTEGER NOT NULL DEFAULT 10;
ALTER TABLE instruments ADD COLUMN min_hold_seconds     INTEGER NOT NULL DEFAULT 30;
ALTER TABLE instruments ADD COLUMN ofi_exit_threshold   REAL    NOT NULL DEFAULT 0.4;
ALTER TABLE instruments ADD COLUMN min_ofi_confirmations INTEGER NOT NULL DEFAULT 3;
