-- Keep historical observations on their unknown account rows. Only source
-- configuration is copied by AccountRegistry after private legacy import.
ALTER TABLE telegram_sources ADD COLUMN legacy_retired INTEGER NOT NULL DEFAULT 0
    CHECK(legacy_retired IN (0, 1));
CREATE TRIGGER telegram_sources_retired_revision
AFTER UPDATE OF legacy_retired ON telegram_sources
WHEN NEW.legacy_retired IS NOT OLD.legacy_retired
BEGIN
    UPDATE telegram_sources SET revision = OLD.revision + 1 WHERE id = NEW.id;
END;
CREATE TABLE legacy_telegram_source_imports (
    account_id TEXT PRIMARY KEY REFERENCES external_accounts(account_id),
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
