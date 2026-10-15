-- A single backend process owns the DB's OS lock; heartbeat is diagnostic and
-- renews active attempts, never evidence that remote resources have stopped.
CREATE TABLE worker_sessions (
    session_id TEXT PRIMARY KEY,
    process_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('LIVE', 'STOPPED', 'ABANDONED')),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    stopped_at TEXT
);
CREATE INDEX worker_sessions_live ON worker_sessions(status);
CREATE UNIQUE INDEX worker_session_single_live ON worker_sessions(status) WHERE status = 'LIVE';

ALTER TABLE download_jobs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count >= 0);

CREATE TABLE telegram_cooldowns (
    account_id TEXT PRIMARY KEY REFERENCES external_accounts(account_id),
    retry_at TEXT NOT NULL,
    error_code TEXT NOT NULL
);

-- Completed payloads may keep seeding, without consuming a download slot.
-- Unknown/failed pushes and STOPPING retain their reservation until confirmed.
ALTER TABLE queue_remote_resources ADD COLUMN download_finished INTEGER NOT NULL DEFAULT 0
    CHECK(download_finished IN (0, 1));
UPDATE queue_remote_resources SET download_finished = 1
WHERE task_id IN (SELECT task_id FROM queue_tasks WHERE status = 'COMPLETED');

ALTER TABLE queue_relocations ADD COLUMN worker_session_id TEXT;
CREATE TRIGGER queue_relocation_session AFTER INSERT ON queue_relocations BEGIN
    UPDATE queue_relocations SET worker_session_id =
        (SELECT session_id FROM worker_sessions WHERE status = 'LIVE')
    WHERE relocation_id = NEW.relocation_id;
END;

-- Exhausted crash recovery still confirms remote stop before becoming failed.
-- Retain the existing account-replacement exception from 035.
DROP TRIGGER queue_stopping_guard;
CREATE TRIGGER queue_stopping_guard BEFORE UPDATE OF state ON download_jobs
WHEN OLD.state = 'STOPPING' AND NEW.state NOT IN ('STOPPING', 'CANCELLED')
  AND NOT (NEW.state IN ('PENDING', 'FAILED') AND EXISTS (
      SELECT 1 FROM queue_operations o JOIN queue_tasks t ON t.task_id = o.task_id
      WHERE o.task_id = OLD.task_id AND o.legacy_job_id = OLD.id
        AND o.attempt_id = t.current_attempt_id AND o.generation + 1 = t.generation
        AND o.status = 'COMPLETED' AND (o.action = 'replace-account' OR
            (NEW.state = 'FAILED' AND o.action = 'restart'
             AND json_extract(o.params_json, '$.recovery_exhausted') = 1
             AND NOT EXISTS(SELECT 1 FROM queue_remote_resources r
                 WHERE r.task_id = OLD.task_id AND r.status <> 'STOP_CONFIRMED')))
  )) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STOP_NOT_CONFIRMED');
END;
