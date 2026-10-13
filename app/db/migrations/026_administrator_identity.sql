-- Login name is mutable; the existing administrator id remains the principal.
CREATE UNIQUE INDEX idx_admin_singleton ON admin_users ((1));
ALTER TABLE admin_users ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE admin_users ADD COLUMN auth_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE api_credentials ADD COLUMN auth_epoch INTEGER NOT NULL DEFAULT 0;

CREATE TABLE browser_sessions (
    session_id TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admin_users(id),
    auth_epoch INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at TEXT
);
