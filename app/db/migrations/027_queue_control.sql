-- download_jobs remains the legacy provider projection, never the task identity.
-- Historical conflicts require operator reconciliation; do not pretend remote
-- transfers have stopped just to make the unique index fit.
CREATE TEMP TABLE queue_migration_guard (valid INTEGER CHECK(valid = 1));
INSERT INTO queue_migration_guard
SELECT 0 FROM download_jobs WHERE provider <> 'CONVERSION'
  AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
GROUP BY candidate_id HAVING count(*) > 1;
DROP TABLE queue_migration_guard;

CREATE TABLE candidate_approvals (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    evidence TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO candidate_approvals(candidate_id, evidence)
SELECT id, 'migration:approved_status' FROM candidates WHERE status = 'APPROVED';
INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
SELECT candidate_id, 'migration:review_action' FROM review_actions
WHERE action IN ('APPROVE', 'AUTO_APPROVE');
CREATE TRIGGER candidate_approval_insert AFTER INSERT ON candidates
WHEN NEW.status = 'APPROVED' BEGIN
    INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
    VALUES(NEW.id, 'approved_status');
END;
CREATE TRIGGER candidate_approval_update AFTER UPDATE OF status ON candidates
WHEN NEW.status = 'APPROVED' BEGIN
    INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
    VALUES(NEW.id, 'approved_status');
END;
CREATE TRIGGER candidate_approval_review AFTER INSERT ON review_actions
WHEN NEW.action IN ('APPROVE', 'AUTO_APPROVE') BEGIN
    INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
    VALUES(NEW.candidate_id, 'review_action');
END;
CREATE TRIGGER candidate_approval_merge AFTER UPDATE OF candidate_id ON review_actions
WHEN NEW.action IN ('APPROVE', 'AUTO_APPROVE') BEGIN
    INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
    VALUES(NEW.candidate_id, 'merged_review_action');
END;

CREATE TABLE queue_tasks (
    task_id TEXT PRIMARY KEY,
    legacy_job_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('DOWNLOAD', 'PACK')),
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    current_attempt_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX queue_download_slot ON queue_tasks(candidate_id)
WHERE kind = 'DOWNLOAD' AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED');
CREATE INDEX queue_tasks_legacy ON queue_tasks(legacy_job_id, created_at);
CREATE TABLE queue_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES queue_tasks(task_id),
    generation INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    UNIQUE(task_id, attempt_no)
);
CREATE TABLE queue_operations (
    operation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    generation INTEGER NOT NULL,
    legacy_job_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    resource_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE queue_command_receipts (
    scope TEXT NOT NULL CHECK(scope = 'administrator'),
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE INDEX queue_operations_task ON queue_operations(task_id, status);
ALTER TABLE download_jobs ADD COLUMN task_id TEXT;
UPDATE download_jobs SET task_id = lower(hex(randomblob(16)));
CREATE UNIQUE INDEX download_jobs_task ON download_jobs(task_id);
INSERT INTO queue_tasks(task_id, legacy_job_id, candidate_id, kind, provider,
                        status, priority, details_json, created_at, updated_at)
SELECT task_id, id, candidate_id,
       CASE WHEN provider = 'CONVERSION' THEN 'PACK' ELSE 'DOWNLOAD' END,
       provider, state, priority, details_json, created_at, updated_at
FROM download_jobs;
-- One reconstructed attempt for an execution in progress; historical counts
-- are not fabricated into invented individual execution records.
INSERT INTO queue_attempts(attempt_id, task_id, generation, attempt_no, kind,
                           provider, status, lease_owner, lease_expires_at, details_json)
SELECT lower(hex(randomblob(16))), task_id, 1, max(attempt_count, 1),
       CASE WHEN provider = 'CONVERSION' THEN 'PACK' ELSE 'DOWNLOAD' END,
       provider, state, lease_owner, lease_expires_at, details_json
FROM download_jobs WHERE state IN ('DOWNLOADING', 'WAITING_TORRENT', 'CONVERSION_RUNNING');
UPDATE queue_tasks SET current_attempt_id =
  (SELECT attempt_id FROM queue_attempts WHERE queue_attempts.task_id = queue_tasks.task_id);

-- This adapter captures every legacy writer, including delegated providers.
-- It is bookkeeping and constraints, not a second scheduler.
CREATE TRIGGER queue_job_insert AFTER INSERT ON download_jobs BEGIN
    UPDATE download_jobs SET task_id = lower(hex(randomblob(16))) WHERE id = NEW.id;
    INSERT INTO queue_tasks(task_id, legacy_job_id, candidate_id, kind, provider,
                            status, priority, details_json)
    SELECT task_id, id, candidate_id,
           CASE WHEN provider = 'CONVERSION' THEN 'PACK' ELSE 'DOWNLOAD' END,
           provider, state, priority, details_json FROM download_jobs WHERE id = NEW.id;
END;
CREATE TRIGGER queue_job_restart AFTER UPDATE OF state ON download_jobs
WHEN OLD.state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED')
  AND NEW.state IN ('PENDING', 'CONVERSION_PENDING') BEGIN
    UPDATE download_jobs SET task_id = lower(hex(randomblob(16))) WHERE id = NEW.id;
    INSERT INTO queue_tasks(task_id, legacy_job_id, candidate_id, kind, provider,
                            status, generation, priority, details_json)
    SELECT j.task_id, j.id, j.candidate_id,
           CASE WHEN j.provider = 'CONVERSION' THEN 'PACK' ELSE 'DOWNLOAD' END,
           j.provider, j.state, t.generation + 1, j.priority, j.details_json
    FROM download_jobs j JOIN queue_tasks t ON t.task_id = OLD.task_id WHERE j.id = NEW.id;
END;
CREATE TRIGGER queue_job_update AFTER UPDATE OF state, priority, details_json, candidate_id ON download_jobs
WHEN NOT (OLD.state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED')
          AND NEW.state IN ('PENDING', 'CONVERSION_PENDING')) BEGIN
    INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
    SELECT NEW.candidate_id, evidence FROM candidate_approvals
    WHERE candidate_id = OLD.candidate_id AND NEW.candidate_id <> OLD.candidate_id;
    UPDATE queue_tasks SET status = NEW.state, priority = NEW.priority,
        candidate_id = NEW.candidate_id, details_json = NEW.details_json,
        revision = revision + CASE WHEN OLD.state <> NEW.state OR OLD.priority <> NEW.priority THEN 1 ELSE 0 END,
        generation = generation + CASE WHEN OLD.state <> NEW.state AND NEW.state IN ('STOPPING', 'CANCELLED') THEN 1 ELSE 0 END,
        updated_at = CURRENT_TIMESTAMP WHERE task_id = NEW.task_id;
    UPDATE queue_attempts SET status = NEW.state, details_json = NEW.details_json,
        error_code = NEW.error_code, error_message = NEW.error_message,
        finished_at = CASE WHEN NEW.state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED',
            'CONVERSION_WAITING_PASSWORD', 'CONVERSION_WAITING_VOLUMES', 'CONVERSION_WAITING_PATH')
            THEN CURRENT_TIMESTAMP ELSE finished_at END
    WHERE attempt_id = (SELECT current_attempt_id FROM queue_tasks WHERE task_id = NEW.task_id);
END;
CREATE TRIGGER queue_job_claim AFTER UPDATE OF state ON download_jobs
WHEN NEW.state IN ('DOWNLOADING', 'CONVERSION_RUNNING') AND OLD.state <> NEW.state BEGIN
    INSERT INTO queue_attempts(attempt_id, task_id, generation, attempt_no, kind, provider,
                               status, lease_owner, lease_expires_at, details_json)
    SELECT lower(hex(randomblob(16))), task_id, generation,
           coalesce((SELECT max(attempt_no) FROM queue_attempts WHERE task_id = t.task_id), 0) + 1,
           kind, provider, NEW.state, NEW.lease_owner, NEW.lease_expires_at, NEW.details_json
    FROM queue_tasks t WHERE task_id = NEW.task_id;
    UPDATE queue_tasks SET current_attempt_id =
        (SELECT attempt_id FROM queue_attempts WHERE task_id = NEW.task_id ORDER BY attempt_no DESC LIMIT 1)
    WHERE task_id = NEW.task_id;
END;
CREATE TRIGGER queue_stopping_guard BEFORE UPDATE OF state ON download_jobs
WHEN OLD.state = 'STOPPING' AND NEW.state NOT IN ('STOPPING', 'CANCELLED') BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STOP_NOT_CONFIRMED');
END;
CREATE TRIGGER queue_remote_restart_guard BEFORE UPDATE OF state ON download_jobs
WHEN NEW.provider = 'EH_TORRENT' AND NEW.state = 'PENDING'
  AND OLD.state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'PAUSED')
  AND OLD.details_json NOT IN ('', '{}') BEGIN
    SELECT RAISE(ABORT, 'QUEUE_REMOTE_RECONCILIATION_REQUIRED');
END;
CREATE TRIGGER queue_cancelled_commit_guard BEFORE UPDATE OF state ON download_jobs
WHEN OLD.state = 'CANCELLED' AND NEW.state NOT IN ('CANCELLED', 'PENDING', 'CONVERSION_PENDING') BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STALE_COMPLETION');
END;
CREATE TRIGGER queue_artifact_insert_guard BEFORE INSERT ON artifacts
WHEN EXISTS(SELECT 1 FROM download_jobs WHERE id = NEW.job_id AND state IN ('STOPPING', 'CANCELLED')) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STALE_ARTIFACT');
END;
CREATE TRIGGER queue_artifact_update_guard BEFORE UPDATE ON artifacts
WHEN EXISTS(SELECT 1 FROM download_jobs WHERE id = NEW.job_id AND state IN ('STOPPING', 'CANCELLED')) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STALE_ARTIFACT');
END;
CREATE TRIGGER queue_active_delete_guard BEFORE DELETE ON download_jobs
WHEN OLD.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED') BEGIN
    SELECT RAISE(ABORT, 'QUEUE_ACTIVE_RECORD_NOT_REMOVABLE');
END;
CREATE TRIGGER queue_job_delete AFTER DELETE ON download_jobs BEGIN
    UPDATE queue_tasks SET status = 'REMOVED', revision = revision + 1,
        generation = generation + 1, updated_at = CURRENT_TIMESTAMP WHERE task_id = OLD.task_id;
END;
