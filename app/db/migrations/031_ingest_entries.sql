-- One identity arbiter for observations before and after candidate creation.
-- Legacy telegram_accounts have no proven external identity; never backfill one.
ALTER TABLE telegram_accounts ADD COLUMN external_account_id TEXT
    REFERENCES external_accounts(account_id);
CREATE UNIQUE INDEX telegram_account_external_identity
    ON telegram_accounts(external_account_id) WHERE external_account_id IS NOT NULL;

CREATE TABLE ingest_entries (
    id INTEGER PRIMARY KEY,
    unique_key TEXT NOT NULL UNIQUE,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    processing_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(processing_state IN ('PENDING', 'PROCESSING', 'LINKED', 'IGNORED', 'REMOVED', 'MERGED', 'BLOCKED')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
    owner TEXT,
    lease_until INTEGER,
    input_source_message_id INTEGER REFERENCES source_messages(id),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    retry_at INTEGER,
    error_code TEXT,
    followup_revision INTEGER NOT NULL DEFAULT 0 CHECK(followup_revision >= 0),
    followup_done_revision INTEGER NOT NULL DEFAULT 0 CHECK(followup_done_revision >= 0 AND followup_done_revision <= followup_revision),
    followup_attempts INTEGER NOT NULL DEFAULT 0 CHECK(followup_attempts >= 0),
    followup_retry_at INTEGER,
    extra_json TEXT NOT NULL DEFAULT '{"key_version":1}' CHECK(json_valid(extra_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK((processing_state = 'PROCESSING' AND owner IS NOT NULL AND lease_until IS NOT NULL)
       OR (processing_state <> 'PROCESSING' AND owner IS NULL AND lease_until IS NULL)),
    CHECK(processing_state <> 'LINKED' OR candidate_id IS NOT NULL)
);
CREATE INDEX ingest_pending ON ingest_entries(processing_state, retry_at, lease_until, id);
CREATE INDEX ingest_candidate ON ingest_entries(candidate_id) WHERE candidate_id IS NOT NULL;
CREATE INDEX ingest_followup_pending ON ingest_entries(followup_retry_at, id)
    WHERE followup_revision > followup_done_revision AND processing_state = 'LINKED';
-- Publication and its follow-up intent commit together, including legacy import.
-- Further observations of an already linked identity do not schedule it again.
CREATE TRIGGER ingest_followup_insert AFTER INSERT ON ingest_entries
WHEN NEW.processing_state = 'LINKED' AND EXISTS(
    SELECT 1 FROM candidates WHERE id = NEW.candidate_id AND ex_gid IS NOT NULL
    AND status IN ('PENDING_REVIEW', 'NEEDS_INFO')) BEGIN
    UPDATE ingest_entries SET followup_revision = followup_revision + 1,
        followup_retry_at = NULL, followup_attempts = 0 WHERE id = NEW.id;
END;
CREATE TRIGGER ingest_followup_link AFTER UPDATE OF candidate_id, processing_state ON ingest_entries
WHEN NEW.processing_state = 'LINKED'
 AND (OLD.processing_state <> 'LINKED' OR OLD.candidate_id IS NOT NEW.candidate_id)
 AND EXISTS(SELECT 1 FROM candidates WHERE id = NEW.candidate_id AND ex_gid IS NOT NULL
    AND status IN ('PENDING_REVIEW', 'NEEDS_INFO')) BEGIN
    UPDATE ingest_entries SET followup_revision = followup_revision + 1,
        followup_retry_at = NULL, followup_attempts = 0 WHERE id = NEW.id;
END;

ALTER TABLE source_messages ADD COLUMN ingest_entry_id INTEGER REFERENCES ingest_entries(id);
ALTER TABLE source_messages ADD COLUMN ingest_generation INTEGER;
ALTER TABLE source_messages ADD COLUMN observation_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE source_messages ADD COLUMN peer_type TEXT;
ALTER TABLE source_messages ADD COLUMN origin_channel_id INTEGER;
ALTER TABLE source_messages ADD COLUMN origin_message_id INTEGER;
ALTER TABLE source_messages ADD COLUMN ex_gid INTEGER;
ALTER TABLE source_messages ADD COLUMN ex_gallery_token TEXT;
ALTER TABLE source_messages ADD COLUMN gallery_from_preview INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_messages ADD COLUMN rule_source_id INTEGER REFERENCES telegram_sources(id) ON DELETE SET NULL;
ALTER TABLE source_messages ADD COLUMN is_forwarded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_messages ADD COLUMN chat_title TEXT NOT NULL DEFAULT '';
UPDATE source_messages SET rule_source_id = (
    SELECT id FROM telegram_sources ts WHERE ts.account_id = source_messages.account_id
    AND ts.chat_id = source_messages.chat_id
);
-- Historical messages did not retain the received title. Use their configured
-- label when known, without inventing an account or changing their identity.
UPDATE source_messages SET chat_title = COALESCE((
    SELECT display_name FROM telegram_sources WHERE id = source_messages.rule_source_id
), CAST(chat_id AS TEXT));
CREATE INDEX ingest_observations ON source_messages(ingest_entry_id, ingest_generation, id);

-- Bot update ids belong to the receiving account, including before parsing.
-- Historical observations remain explicitly unknown instead of being guessed.
ALTER TABLE telegram_bot_updates RENAME TO telegram_bot_updates_legacy;
DROP INDEX idx_telegram_bot_updates_pending;
CREATE TABLE telegram_bot_updates (
    account_scope TEXT NOT NULL DEFAULT 'legacy',
    update_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT,
    processing_result TEXT,
    processing_reason TEXT,
    external_account_id TEXT REFERENCES external_accounts(account_id),
    PRIMARY KEY(account_scope, update_id),
    CHECK((account_scope = 'legacy' AND external_account_id IS NULL)
       OR (external_account_id IS NOT NULL AND account_scope = external_account_id))
);
INSERT INTO telegram_bot_updates(update_id, payload_json, received_at, processed_at, processing_result, processing_reason)
SELECT update_id, payload_json, received_at, processed_at, processing_result, processing_reason FROM telegram_bot_updates_legacy;
DROP TABLE telegram_bot_updates_legacy;
CREATE INDEX idx_telegram_bot_updates_pending ON telegram_bot_updates(account_scope, update_id) WHERE processed_at IS NULL;

-- Invalidate the processing generation inside the actual business removal.
-- Source rows retain enough identity to keep old deliveries from resurrecting it.
CREATE TRIGGER ingest_candidate_removed BEFORE DELETE ON candidates BEGIN
    UPDATE ingest_entries SET processing_state = 'REMOVED', candidate_id = NULL,
        revision = revision + 1, owner = NULL, lease_until = NULL, retry_at = NULL,
        extra_json = json_set(extra_json, '$.removed_at', unixepoch()),
        updated_at = CURRENT_TIMESTAMP WHERE candidate_id = OLD.id;
END;

-- Identity changes survive a restart while P3 confirms the old execution's stop.
-- Keep ids after a merge/removal so the result remains inspectable.
CREATE TABLE ingest_reference_changes (
    operation_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    candidate_id INTEGER NOT NULL,
    old_gid INTEGER,
    old_token TEXT,
    gallery_id INTEGER NOT NULL CHECK(gallery_id > 0),
    gallery_token TEXT,
    target_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'COMPLETED', 'FAILED')),
    result_candidate_id INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX ingest_reference_pending ON ingest_reference_changes(candidate_id) WHERE status = 'PENDING';
CREATE INDEX ingest_reference_target ON ingest_reference_changes(target_key) WHERE status = 'PENDING';

CREATE TABLE ingest_observation_changes (
    source_message_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    candidate_id INTEGER NOT NULL,
    original_candidate_id INTEGER NOT NULL,
    target_key TEXT,
    observation_revision INTEGER NOT NULL,
    merge_all INTEGER NOT NULL CHECK(merge_all IN (0, 1)),
    message_json TEXT NOT NULL CHECK(json_valid(message_json)),
    task_ids_json TEXT NOT NULL CHECK(json_valid(task_ids_json)),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'COMPLETED', 'FAILED')),
    result_candidate_id INTEGER,
    error_code TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ingest_command_receipts (
    scope TEXT NOT NULL DEFAULT 'administrator' CHECK(scope = 'administrator'),
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE INDEX ingest_observation_target ON ingest_observation_changes(target_key)
    WHERE status = 'PENDING' AND target_key IS NOT NULL;
CREATE TRIGGER ingest_reference_job_insert BEFORE INSERT ON download_jobs
WHEN NEW.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED', 'STOPPING')
 AND (EXISTS(SELECT 1 FROM ingest_reference_changes WHERE candidate_id = NEW.candidate_id AND status = 'PENDING')
 OR EXISTS(SELECT 1 FROM ingest_observation_changes WHERE candidate_id = NEW.candidate_id AND status = 'PENDING'
   AND (merge_all = 1 OR source_message_id = json_extract(NEW.details_json, '$.observation_id')))) BEGIN
    SELECT RAISE(ABORT, 'INGEST_IDENTITY_CHANGING');
END;
CREATE TRIGGER ingest_reference_job_update BEFORE UPDATE OF state ON download_jobs
WHEN NEW.state <> OLD.state
 AND NEW.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED', 'STOPPING')
 AND (EXISTS(SELECT 1 FROM ingest_reference_changes WHERE candidate_id = NEW.candidate_id AND status = 'PENDING')
 OR EXISTS(SELECT 1 FROM ingest_observation_changes WHERE candidate_id = NEW.candidate_id AND status = 'PENDING'
   AND (merge_all = 1 OR source_message_id = json_extract(NEW.details_json, '$.observation_id')))) BEGIN
    SELECT RAISE(ABORT, 'INGEST_IDENTITY_CHANGING');
END;
