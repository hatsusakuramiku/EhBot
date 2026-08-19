ALTER TABLE source_messages ADD COLUMN filter_result TEXT NOT NULL DEFAULT 'ACCEPT';
ALTER TABLE source_messages ADD COLUMN filter_reason TEXT NOT NULL DEFAULT '';

UPDATE telegram_sources
SET enabled = 0;
