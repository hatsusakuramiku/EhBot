CREATE TABLE archive_tool_profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    backend TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('BUILTIN', 'CLI', 'BRIDGE')),
    executable_path TEXT,
    supported_formats TEXT NOT NULL DEFAULT '[]',
    timeout_seconds INTEGER NOT NULL DEFAULT 600,
    capabilities TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO archive_tool_profiles
    (name, backend, kind, executable_path, supported_formats,
     timeout_seconds, capabilities, enabled)
VALUES
    ('zipfile-default', 'zipfile', 'BUILTIN', NULL, '["zip"]', 600,
     '["stream", "zip_password"]', 1),
    ('7zz-default', 'seven_zip', 'CLI', '7zz', '["rar", "7z", "zip"]', 900,
     '["password", "volumes", "managed_install"]', 1);

CREATE TABLE archive_passwords (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    secret_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_success_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_archive_passwords_order
    ON archive_passwords (enabled, priority, id);

CREATE TABLE archive_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);