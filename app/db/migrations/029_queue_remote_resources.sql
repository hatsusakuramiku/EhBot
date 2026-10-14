-- Remote push intentions outlive an interrupted HTTP request and the business row.
CREATE TABLE queue_remote_resources (
    resource_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    legacy_job_id INTEGER NOT NULL,
    instance_url TEXT NOT NULL,
    digest TEXT NOT NULL,
    ownership_tag TEXT NOT NULL,
    ownership TEXT NOT NULL CHECK(ownership IN ('OWNED', 'SHARED', 'UNKNOWN')),
    status TEXT NOT NULL CHECK(status IN ('INTENDED', 'REGISTERED', 'STOP_CONFIRMED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX queue_remote_resources_job ON queue_remote_resources(legacy_job_id, status);

-- Interrupted push acceptance must be reconciled before requeue, even if a
-- legacy lease writer tries to bypass the operation processor.
CREATE TRIGGER queue_remote_reclaim_guard BEFORE UPDATE OF state ON download_jobs
WHEN OLD.provider = 'EH_TORRENT' AND OLD.state = 'DOWNLOADING' AND NEW.state = 'PENDING'
  AND (EXISTS(SELECT 1 FROM queue_remote_resources r
              WHERE r.task_id = OLD.task_id AND r.status <> 'STOP_CONFIRMED')
       OR (json_valid(OLD.details_json) AND json_extract(OLD.details_json, '$.hash') IS NOT NULL)) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_REMOTE_RECONCILIATION_REQUIRED');
END;

CREATE TABLE queue_attempt_paths (
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    PRIMARY KEY(task_id, attempt_id, directory)
);

-- A cancelled remote job can require a fresh stop check before retry. Only a
-- durable command for the current identity may make that transition; stale
-- providers still cannot resurrect a cancellation by writing STOPPING.
DROP TRIGGER queue_cancelled_commit_guard;
CREATE TRIGGER queue_cancelled_commit_guard BEFORE UPDATE OF state ON download_jobs
WHEN OLD.state = 'CANCELLED'
  AND NEW.state NOT IN ('CANCELLED', 'PENDING', 'CONVERSION_PENDING')
  AND NOT (NEW.state = 'STOPPING' AND EXISTS(
    SELECT 1 FROM queue_operations o JOIN queue_tasks t ON t.task_id = o.task_id
    WHERE o.task_id = OLD.task_id AND o.legacy_job_id = OLD.id
      AND o.generation = t.generation AND o.action IN ('retry', 'restart')
      AND o.status NOT IN ('COMPLETED', 'FAILED')
  )) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STALE_COMPLETION');
END;

-- The reservation and file manifest survive deletion of candidate/job rows.
CREATE TABLE queue_removals (
    removal_id TEXT PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    ex_gid INTEGER,
    torrent_hash TEXT,
    delete_files INTEGER NOT NULL CHECK(delete_files IN (0, 1)),
    purge_candidate INTEGER NOT NULL CHECK(purge_candidate IN (0, 1)),
    targets_json TEXT NOT NULL,
    directories_json TEXT NOT NULL,
    task_ids_json TEXT NOT NULL,
    audit_id INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'COMPLETED')),
    deleted_json TEXT NOT NULL DEFAULT '[]',
    failed_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX queue_removals_pending ON queue_removals(status, candidate_id);
CREATE VIEW queue_blocked_candidates AS
SELECT DISTINCT c.id AS candidate_id FROM candidates c JOIN queue_removals r
  ON r.status = 'PENDING' AND (r.candidate_id = c.id
     OR (r.ex_gid IS NOT NULL AND r.ex_gid = c.ex_gid)
     OR (r.torrent_hash IS NOT NULL AND lower(r.torrent_hash) = lower(c.torrent_hash)));

-- New tasks may queue with fresh UUIDs while the independent removal reserves
-- the work. Every claim path must wait, including direct compatibility writes.
CREATE TRIGGER queue_removal_claim_guard BEFORE UPDATE OF state ON download_jobs
WHEN NEW.state IN ('DOWNLOADING', 'CONVERSION_RUNNING') AND OLD.state <> NEW.state
  AND EXISTS(SELECT 1 FROM queue_blocked_candidates WHERE candidate_id = NEW.candidate_id) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_REMOVAL_NOT_CONFIRMED');
END;
DROP TRIGGER queue_active_delete_guard;
CREATE TRIGGER queue_removal_insert_guard BEFORE INSERT ON download_jobs
WHEN NEW.state IN ('DOWNLOADING', 'CONVERSION_RUNNING')
  AND EXISTS(SELECT 1 FROM queue_blocked_candidates WHERE candidate_id = NEW.candidate_id) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_REMOVAL_NOT_CONFIRMED');
END;
CREATE TRIGGER queue_active_delete_guard BEFORE DELETE ON download_jobs
WHEN OLD.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED')
  AND NOT EXISTS(SELECT 1 FROM queue_operations o JOIN queue_removals r
    ON r.removal_id = json_extract(o.params_json, '$.removal_id')
    WHERE o.task_id = OLD.task_id AND o.legacy_job_id = OLD.id AND o.action = 'remove'
      AND o.status <> 'FAILED' AND r.status = 'PENDING') BEGIN
    SELECT RAISE(ABORT, 'QUEUE_ACTIVE_RECORD_NOT_REMOVABLE');
END;
