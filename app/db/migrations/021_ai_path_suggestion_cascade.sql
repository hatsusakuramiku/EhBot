-- `ai_path_suggestions` was added (018) with a plain `REFERENCES candidates(id)`:
-- no `ON DELETE CASCADE`, unlike every other child of `candidates`. Two existing
-- code paths delete a candidate row, and both then failed with
-- `FOREIGN KEY constraint failed` whenever the candidate had an AI path cached:
--
--  * the merge in `_save_candidate_message_sync`, which re-points
--    `candidate_messages` / `metadata_values` / `review_actions` / `download_jobs`
--    at the surviving candidate and then deletes the absorbed one -- reached when
--    a message joins one candidate by reply/media-group while its gallery id
--    matches a second one. That path runs at startup (`_ingest_pending` before
--    the server accepts a request), so the failure was not a bad response: the
--    service refused to start at all.
--  * the edit-removal path, which deletes a candidate once its last message is
--    deactivated.
--
-- The merge also moves the suggestion now (mirroring `metadata_values`, which it
-- already did), so an AI generation paid for before a merge is not thrown away
-- when the survivor has none of its own. The cascade is the structural half:
-- every other delete of a candidate works without knowing this table exists.
--
-- SQLite cannot alter a foreign key in place, so the table is rebuilt. The
-- column list is spelled out rather than `SELECT *` so a future column order
-- change cannot silently mis-map values here.

CREATE TABLE ai_path_suggestions_cascaded (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    directory TEXT NOT NULL,
    filename TEXT NOT NULL,
    provider_id INTEGER,
    model_name TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO ai_path_suggestions_cascaded (
    candidate_id, fingerprint, prompt_hash, relative_path, directory, filename,
    provider_id, model_name, attempts, created_at, updated_at
)
SELECT
    candidate_id, fingerprint, prompt_hash, relative_path, directory, filename,
    provider_id, model_name, attempts, created_at, updated_at
FROM ai_path_suggestions;

DROP TABLE ai_path_suggestions;

ALTER TABLE ai_path_suggestions_cascaded RENAME TO ai_path_suggestions;
