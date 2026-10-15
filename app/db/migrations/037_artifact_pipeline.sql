-- artifacts remains the current projection; a pack names an immutable version.
ALTER TABLE artifacts ADD COLUMN artifact_id TEXT;
CREATE TABLE artifact_versions (
    artifact_id TEXT PRIMARY KEY,
    projection_id INTEGER NOT NULL,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    job_id INTEGER NOT NULL,
    task_id TEXT,
    producer_attempt_id TEXT,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    page_count INTEGER,
    library_relative_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
UPDATE artifacts SET artifact_id = lower(hex(randomblob(16)));
-- Historical files do not prove which execution produced their bytes.
INSERT INTO artifact_versions(artifact_id, projection_id, candidate_id, job_id,
    task_id, artifact_type, path, sha256, size_bytes, page_count, library_relative_path)
SELECT a.artifact_id, a.id, j.candidate_id, j.id, j.task_id, a.artifact_type,
    a.path, a.sha256, a.size_bytes, a.page_count, a.library_relative_path
FROM artifacts a JOIN download_jobs j ON j.id = a.job_id;
CREATE INDEX artifact_versions_task ON artifact_versions(task_id, artifact_type);
-- Ownership can move during an approved candidate merge; content/producer
-- facts cannot. Removing a work detaches ownership without erasing history.
CREATE TRIGGER artifact_versions_immutable BEFORE UPDATE OF artifact_id,
    projection_id, job_id, task_id, producer_attempt_id, artifact_type, path,
    sha256, size_bytes, page_count, library_relative_path, created_at ON artifact_versions BEGIN
    SELECT RAISE(ABORT, 'ARTIFACT_VERSION_IMMUTABLE');
END;
CREATE TRIGGER artifact_versions_owner_move AFTER UPDATE OF candidate_id ON download_jobs BEGIN
    UPDATE artifact_versions SET candidate_id = NEW.candidate_id
        WHERE job_id = NEW.id AND candidate_id = OLD.candidate_id;
END;
CREATE TRIGGER artifact_versions_owner_remove AFTER DELETE ON download_jobs BEGIN
    UPDATE artifact_versions SET candidate_id = NULL
        WHERE job_id = OLD.id AND candidate_id = OLD.candidate_id;
END;
CREATE TRIGGER artifact_version_insert AFTER INSERT ON artifacts BEGIN
    UPDATE artifacts SET artifact_id = lower(hex(randomblob(16))) WHERE id = NEW.id;
    INSERT INTO artifact_versions(artifact_id, projection_id, candidate_id, job_id,
        task_id, producer_attempt_id, artifact_type, path, sha256, size_bytes,
        page_count, library_relative_path)
    SELECT a.artifact_id, a.id, j.candidate_id, j.id, j.task_id, t.current_attempt_id,
        a.artifact_type, a.path, a.sha256, a.size_bytes, a.page_count, a.library_relative_path
    FROM artifacts a JOIN download_jobs j ON j.id = a.job_id
    JOIN queue_tasks t ON t.task_id = j.task_id WHERE a.id = NEW.id;
END;
CREATE TRIGGER artifact_version_update AFTER UPDATE OF path, sha256, size_bytes,
    page_count, library_relative_path ON artifacts BEGIN
    UPDATE artifacts SET artifact_id = lower(hex(randomblob(16))) WHERE id = NEW.id;
    INSERT INTO artifact_versions(artifact_id, projection_id, candidate_id, job_id,
        task_id, producer_attempt_id, artifact_type, path, sha256, size_bytes,
        page_count, library_relative_path)
    SELECT a.artifact_id, a.id, j.candidate_id, j.id, j.task_id, t.current_attempt_id,
        a.artifact_type, a.path, a.sha256, a.size_bytes, a.page_count, a.library_relative_path
    FROM artifacts a JOIN download_jobs j ON j.id = a.job_id
    JOIN queue_tasks t ON t.task_id = j.task_id WHERE a.id = NEW.id;
END;

-- Defaults are captured at enqueue, never re-read when the transfer finishes.
CREATE TABLE queue_pack_intents (
    task_id TEXT PRIMARY KEY REFERENCES queue_tasks(task_id),
    pack_requested INTEGER NOT NULL CHECK(pack_requested IN (0, 1)),
    input_artifact_id TEXT REFERENCES artifact_versions(artifact_id),
    pack_task_id TEXT,
    status TEXT NOT NULL DEFAULT 'WAITING'
        CHECK(status IN ('WAITING', 'READY', 'QUEUED', 'CANCELLED'))
);
INSERT INTO queue_pack_intents(task_id, pack_requested)
SELECT task_id, coalesce((SELECT value NOT IN ('0', 'false', 'no')
    FROM archive_settings WHERE key = 'auto_pack_after_download'), 0)
    OR (provider = 'EH_TORRENT' AND coalesce((SELECT value IN ('1', 'true', 'yes')
        FROM archive_settings WHERE key = 'torrent_auto_pack'), 0))
FROM queue_tasks WHERE kind = 'DOWNLOAD'
    AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED');
CREATE TRIGGER queue_pack_capture AFTER INSERT ON queue_tasks
WHEN NEW.kind = 'DOWNLOAD' BEGIN
    UPDATE queue_pack_intents SET status = 'CANCELLED'
    WHERE status IN ('WAITING', 'READY') AND task_id IN (
        SELECT task_id FROM queue_tasks WHERE candidate_id = NEW.candidate_id AND task_id <> NEW.task_id
    );
    INSERT INTO queue_pack_intents(task_id, pack_requested)
    VALUES(NEW.task_id, coalesce((SELECT value NOT IN ('0', 'false', 'no')
        FROM archive_settings WHERE key = 'auto_pack_after_download'), 0)
        OR (NEW.provider = 'EH_TORRENT' AND coalesce((SELECT value IN ('1', 'true', 'yes')
            FROM archive_settings WHERE key = 'torrent_auto_pack'), 0)));
END;
CREATE TRIGGER queue_pack_artifact AFTER INSERT ON artifact_versions
WHEN NEW.artifact_type = 'ARCHIVE' BEGIN
    UPDATE queue_pack_intents SET input_artifact_id = NEW.artifact_id,
        status = CASE WHEN EXISTS(SELECT 1 FROM queue_tasks
            WHERE task_id = NEW.task_id AND status = 'COMPLETED')
            THEN 'READY' ELSE 'WAITING' END
    WHERE task_id = NEW.task_id AND status IN ('WAITING', 'READY');
END;
CREATE TRIGGER queue_pack_complete AFTER UPDATE OF status ON queue_tasks
WHEN NEW.kind = 'DOWNLOAD' BEGIN
    UPDATE queue_pack_intents SET status = CASE
        WHEN NEW.status IN ('CANCELLED', 'REMOVED') THEN 'CANCELLED'
        WHEN NEW.status = 'COMPLETED' AND input_artifact_id IS NOT NULL THEN 'READY'
        ELSE status END
    WHERE task_id = NEW.task_id AND status IN ('WAITING', 'READY');
END;

-- A prepared replacement survives loss of the publishing process. The backup
-- remains until the artifact, attempt outcome and journal commit together.
CREATE TABLE queue_publications (
    publication_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    input_artifact_id TEXT,
    staged_path TEXT NOT NULL,
    destination TEXT NOT NULL,
    backup_path TEXT,
    old_sha256 TEXT,
    new_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    library_relative_path TEXT,
    details_json TEXT NOT NULL,
    source_path TEXT,
    keep_original INTEGER NOT NULL CHECK(keep_original IN (0, 1)),
    cleanup_complete INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_complete IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'PREPARED'
        CHECK(status IN ('PREPARED', 'COMMITTED', 'ROLLED_BACK', 'RECOVERY_REQUIRED')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX queue_publication_destination ON queue_publications(destination)
    WHERE status IN ('PREPARED', 'RECOVERY_REQUIRED');

-- Rename/refile keeps the same bytes. An uncommitted filesystem rename must
-- be reversed before reclaim/removal; a committed path and pin move together.
CREATE TABLE queue_relocations (
    relocation_id TEXT PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    task_id TEXT,
    attempt_id TEXT,
    input_artifact_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    destination TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PREPARED'
        CHECK(status IN ('PREPARED', 'COMMITTED', 'ROLLED_BACK', 'RECOVERY_REQUIRED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX queue_relocation_candidate ON queue_relocations(candidate_id)
    WHERE status IN ('PREPARED', 'RECOVERY_REQUIRED');
CREATE UNIQUE INDEX queue_relocation_destination ON queue_relocations(destination)
    WHERE status IN ('PREPARED', 'RECOVERY_REQUIRED');

CREATE TABLE manual_download_intents (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    retry_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);

-- Bind legacy pending packs once, without inventing historical producers. If
-- a newer download is active, its intent owns the dependency instead of A.
UPDATE queue_pack_intents SET pack_requested = 1 WHERE task_id IN (
    SELECT t.task_id FROM queue_tasks t JOIN download_jobs p ON p.candidate_id = t.candidate_id
    WHERE p.provider = 'CONVERSION' AND p.state IN ('CONVERSION_PENDING', 'CONVERSION_RUNNING')
        AND coalesce(json_extract(p.details_json, '$.refile'), 0) = 0
        AND t.kind = 'DOWNLOAD' AND t.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED')
);
UPDATE download_jobs SET details_json = json_set(details_json, '$.input_artifact_id', (
    SELECT a.artifact_id FROM artifacts a JOIN download_jobs d ON d.id = a.job_id
    WHERE d.candidate_id = download_jobs.candidate_id AND d.state = 'COMPLETED'
        AND a.artifact_type = 'ARCHIVE' ORDER BY d.id DESC LIMIT 1
)) WHERE provider = 'CONVERSION' AND state IN ('CONVERSION_PENDING', 'CONVERSION_RUNNING',
    'CONVERSION_WAITING_PASSWORD', 'CONVERSION_WAITING_VOLUMES', 'CONVERSION_WAITING_PATH')
    AND coalesce(json_extract(details_json, '$.refile'), 0) = 0
    AND NOT EXISTS(SELECT 1 FROM queue_tasks t WHERE t.candidate_id = download_jobs.candidate_id
        AND t.kind = 'DOWNLOAD' AND t.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED'));

UPDATE download_jobs SET details_json = json_set(details_json, '$.input_artifact_id', (
    SELECT a.artifact_id FROM artifacts a WHERE a.job_id = download_jobs.id
        AND a.artifact_type = 'CBZ' LIMIT 1
)) WHERE provider = 'CONVERSION' AND coalesce(json_extract(details_json, '$.refile'), 0) = 1
    AND state IN ('CONVERSION_PENDING', 'CONVERSION_RUNNING', 'CONVERSION_WAITING_PATH');
