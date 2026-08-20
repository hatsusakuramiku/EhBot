CREATE TABLE auto_approval_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    version INTEGER NOT NULL DEFAULT 1,
    condition_json TEXT NOT NULL,
    dsl_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_auto_approval_rules_enabled_priority
    ON auto_approval_rules (enabled, priority, id);
