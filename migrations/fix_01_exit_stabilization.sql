-- FIX_01: Добавление полей стабилизации выхода по OFI
-- База данных: MySQL
--
-- Применить на сервере:
--   mysql -u trading_user -p trading_bot < migrations/fix_01_exit_stabilization.sql
-- или через docker:
--   docker exec -i <container> mysql -u trading_user -p trading_bot < migrations/fix_01_exit_stabilization.sql

ALTER TABLE instruments
    ADD COLUMN ofi_smooth_window     INT           NOT NULL DEFAULT 10    COMMENT 'Окно сглаживания OFI (кол-во снимков стакана)',
    ADD COLUMN min_hold_seconds      INT           NOT NULL DEFAULT 30    COMMENT 'Мин. время удержания позиции в секундах',
    ADD COLUMN ofi_exit_threshold    DOUBLE        NOT NULL DEFAULT 0.4   COMMENT 'Порог OFI для выхода (может быть ниже порога входа)',
    ADD COLUMN min_ofi_confirmations INT           NOT NULL DEFAULT 3     COMMENT 'Кол-во подряд идущих апдейтов с OFI против позиции для выхода';
