-- Mobile / API credentials.
--
-- One table holds both credential families, because they share a wire format
-- (`<public_id>.<secret>`) and one verification path:
--
--   * `api_key`         -- the single long-lived key the operator mints in the
--                          web UI; `expires_at` is always NULL;
--   * `access`/`refresh` -- the pair a password login issues, refresh rotating.
--
-- Only `sha256(secret)` is stored; the plaintext exists in the response that
-- minted it and nowhere else. `family_id` groups an access/refresh pair (and its
-- rotated descendants) so «log out this device» and «password changed» can
-- revoke a whole family, while `rotated_from` keeps the refresh chain for audit.
CREATE TABLE api_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('api_key', 'access', 'refresh')),
    label        TEXT NOT NULL DEFAULT '',
    public_id    TEXT NOT NULL UNIQUE,
    secret_hash  TEXT NOT NULL,
    family_id    TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,
    expires_at   INTEGER,
    revoked_at   TEXT,
    rotated_from INTEGER REFERENCES api_credentials(id)
);

-- "At most one valid API key at a time" is a database invariant, not a rule the
-- route is trusted to remember: re-minting must revoke the old row first.
CREATE UNIQUE INDEX idx_api_credentials_single_key
    ON api_credentials(kind) WHERE kind = 'api_key' AND revoked_at IS NULL;
