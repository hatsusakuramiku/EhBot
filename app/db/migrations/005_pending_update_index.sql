CREATE INDEX IF NOT EXISTS idx_telegram_bot_updates_pending
ON telegram_bot_updates(update_id)
WHERE processed_at IS NULL;
