-- Persistent fair starting point for provider account pools.  A sequence,
-- rather than wall-clock timestamps, keeps concurrent requests deterministic.
CREATE TABLE account_rotation_state (
    kind TEXT PRIMARY KEY CHECK(kind IN ('TG_BOT', 'TG_USER', 'EH')),
    next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence > 0)
);
CREATE TABLE account_rotation (
    account_id TEXT PRIMARY KEY REFERENCES external_accounts(account_id),
    last_sequence INTEGER,
    cooldown_until TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    capabilities_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE eh_shared_backoff (
    scope TEXT PRIMARY KEY CHECK(scope IN ('SITE', 'GDATA')),
    cooldown_until TEXT NOT NULL,
    error_code TEXT NOT NULL
);
CREATE TABLE eh_account_health (
    account_id TEXT NOT NULL REFERENCES external_accounts(account_id),
    credential_revision INTEGER NOT NULL CHECK(credential_revision > 0),
    capability TEXT NOT NULL CHECK(capability IN ('AUTH', 'METADATA', 'ARCHIVE', 'TORRENT')),
    invalid INTEGER NOT NULL CHECK(invalid IN (0, 1)),
    cooldown_until TEXT,
    error_code TEXT NOT NULL,
    PRIMARY KEY(account_id, credential_revision, capability),
    CHECK(invalid = 1 OR cooldown_until IS NOT NULL)
);

-- Read-only metadata operations retain their finite account/version snapshot
-- and safe outcomes. Paid archive intents have a separate lifecycle.
CREATE TABLE eh_metadata_operations (
    operation_id TEXT PRIMARY KEY,
    gid INTEGER NOT NULL CHECK(gid > 0),
    candidates_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'UNKNOWN')),
    error_code TEXT,
    results_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE TABLE eh_metadata_attempts (
    operation_id TEXT NOT NULL REFERENCES eh_metadata_operations(operation_id),
    account_id TEXT NOT NULL REFERENCES external_accounts(account_id),
    credential_revision INTEGER NOT NULL CHECK(credential_revision > 0),
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    status TEXT NOT NULL CHECK(status IN ('STARTED', 'SUCCEEDED', 'FAILED', 'UNAVAILABLE', 'CANCELLED', 'UNKNOWN')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    PRIMARY KEY(operation_id, account_id),
    UNIQUE(operation_id, ordinal)
);

-- Paid archive requests outlive queue cancellation/removal. No gallery token,
-- Cookie or signed download URL is stored here; locators live in SecretStore.
CREATE TABLE eh_archive_intents (
    intent_id TEXT PRIMARY KEY,
    gid INTEGER NOT NULL CHECK(gid > 0),
    account_id TEXT NOT NULL REFERENCES external_accounts(account_id),
    credential_revision INTEGER NOT NULL CHECK(credential_revision > 0),
    task_id TEXT,
    attempt_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('REQUESTED', 'READY', 'REJECTED', 'UNKNOWN')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX eh_archive_unresolved_gid ON eh_archive_intents(gid)
    WHERE status IN ('REQUESTED', 'UNKNOWN');
