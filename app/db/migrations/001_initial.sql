CREATE TABLE IF NOT EXISTS telegram_accounts (
    id INTEGER PRIMARY KEY,
    account_type TEXT NOT NULL CHECK (account_type IN ('BOT', 'USER')),
    display_name TEXT NOT NULL,
    session_path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'DISCONNECTED',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS telegram_sources (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_accounts(id),
    source_type TEXT NOT NULL CHECK (source_type IN ('CHANNEL', 'PRIVATE_CHAT')),
    chat_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    rules_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS source_messages (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_accounts(id),
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id INTEGER,
    reply_to_message_id INTEGER,
    media_group_id TEXT,
    message_text TEXT,
    attachment_json TEXT NOT NULL DEFAULT '[]',
    file_unique_id TEXT,
    message_state TEXT NOT NULL DEFAULT 'ACTIVE',
    message_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    ex_gid INTEGER,
    ex_gallery_token TEXT,
    filter_result TEXT,
    filter_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ex_gid, ex_gallery_token)
);

CREATE TABLE IF NOT EXISTS candidate_messages (
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    source_message_id INTEGER NOT NULL REFERENCES source_messages(id),
    PRIMARY KEY (candidate_id, source_message_id)
);

CREATE TABLE IF NOT EXISTS metadata_values (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    value_source TEXT NOT NULL,
    confidence REAL,
    is_manual INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (candidate_id, field_name, value_source)
);

CREATE TABLE IF NOT EXISTS review_actions (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    action TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS download_jobs (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    retry_at TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES download_jobs(id),
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_id, artifact_type)
);
