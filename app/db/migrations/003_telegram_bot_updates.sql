CREATE TABLE IF NOT EXISTS telegram_bot_updates (
    update_id INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
