-- Account configuration belongs to the one administrator, independently of
-- runtime listeners. Secret values stay in immutable private files.
CREATE TABLE external_accounts (
    account_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('TG_BOT', 'TG_USER', 'EH')),
    label TEXT NOT NULL,
    remote_identity TEXT,
    platform_name TEXT,
    legacy_slot TEXT UNIQUE CHECK(legacy_slot IS NULL OR legacy_slot = kind),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    activation_requested INTEGER NOT NULL DEFAULT 0 CHECK(activation_requested IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    credential_revision INTEGER NOT NULL DEFAULT 1 CHECK(credential_revision > 0),
    verified_credential_revision INTEGER,
    stop_operation_id TEXT,
    credentials_ref TEXT NOT NULL,
    configured_fields_json TEXT NOT NULL DEFAULT '[]',
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(enabled = 0 OR remote_identity IS NOT NULL)
);
CREATE UNIQUE INDEX external_accounts_enabled_identity
    ON external_accounts(kind, remote_identity)
    WHERE enabled = 1 AND deleted_at IS NULL AND remote_identity IS NOT NULL;
CREATE TABLE external_account_credentials (
    account_id TEXT NOT NULL REFERENCES external_accounts(account_id),
    credential_revision INTEGER NOT NULL CHECK(credential_revision > 0),
    secret_ref TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(account_id, credential_revision)
);
-- A stop intention remains durable while listeners/requests/remote resources
-- are not yet confirmed stopped. Pending edits contain references, not secrets.
CREATE TABLE account_operations (
    operation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES external_accounts(account_id),
    action TEXT NOT NULL CHECK(action IN ('disable', 'edit', 'delete')),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'COMPLETED')),
    credential_revision INTEGER NOT NULL,
    next_credentials_ref TEXT,
    next_fields_json TEXT,
    next_label TEXT,
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX account_operations_pending
    ON account_operations(account_id) WHERE status = 'PENDING';
CREATE TABLE account_execution_bindings (
    attempt_id TEXT NOT NULL REFERENCES queue_attempts(attempt_id),
    account_id TEXT NOT NULL,
    credential_revision INTEGER NOT NULL,
    PRIMARY KEY(attempt_id, account_id),
    FOREIGN KEY(account_id, credential_revision)
        REFERENCES external_account_credentials(account_id, credential_revision)
);
CREATE TABLE account_stop_dependencies (
    operation_id TEXT NOT NULL REFERENCES account_operations(operation_id),
    queue_operation_id TEXT NOT NULL REFERENCES queue_operations(operation_id),
    PRIMARY KEY(operation_id, queue_operation_id)
);
CREATE INDEX account_execution_bindings_account ON account_execution_bindings(account_id);
CREATE TABLE account_command_receipts (
    scope TEXT NOT NULL DEFAULT 'administrator' CHECK(scope = 'administrator'),
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(scope, idempotency_key)
);
