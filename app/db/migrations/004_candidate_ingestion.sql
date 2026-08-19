ALTER TABLE telegram_bot_updates ADD COLUMN processed_at TEXT;
ALTER TABLE telegram_bot_updates ADD COLUMN processing_result TEXT;
ALTER TABLE telegram_bot_updates ADD COLUMN processing_reason TEXT;
