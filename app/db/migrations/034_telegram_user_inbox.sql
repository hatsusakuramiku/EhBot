-- MTProto has no server-side getUpdates inbox. Keep its normalized input and
-- source cursor in one local transaction, before any paid admission work.
CREATE TABLE telegram_user_inbox (
    id INTEGER PRIMARY KEY,
    account_scope TEXT NOT NULL,
    external_account_id TEXT REFERENCES external_accounts(account_id),
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_scope, chat_id, message_id)
);
CREATE INDEX telegram_user_inbox_pending ON telegram_user_inbox(account_scope, chat_id, id);
