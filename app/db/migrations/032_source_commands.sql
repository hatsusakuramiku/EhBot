-- P6 configuration concurrency. Runtime cursors do not change edit versions.
ALTER TABLE telegram_sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
CREATE TRIGGER telegram_sources_config_revision
AFTER UPDATE OF source_type, display_name, enabled, rules_json, dismissed, account_id, chat_id
ON telegram_sources
WHEN NEW.source_type IS NOT OLD.source_type
  OR NEW.display_name IS NOT OLD.display_name
  OR NEW.enabled IS NOT OLD.enabled
  OR NEW.rules_json IS NOT OLD.rules_json
  OR NEW.dismissed IS NOT OLD.dismissed
  OR NEW.account_id IS NOT OLD.account_id
  OR NEW.chat_id IS NOT OLD.chat_id
BEGIN
    UPDATE telegram_sources SET revision = OLD.revision + 1 WHERE id = NEW.id;
END;

CREATE TABLE source_command_receipts (
    scope TEXT NOT NULL DEFAULT 'administrator' CHECK(scope = 'administrator'),
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(scope, idempotency_key)
);
