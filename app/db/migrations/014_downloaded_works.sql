-- The 已下载内容 domain: a rename/relocate audit column and a delete guard.
--
-- Restores the book-management surface that was cut on 2026-08-26 and
-- reinstated on 2026-08-28 by operator instruction, following the requirements
-- document's §1.3.1: a page listing downloaded works with delete, rename,
-- relocate, repack and re-download.
--
-- `library_relative_path` records where a repack must land when the operator has
-- renamed or moved the book by hand. Without it the next repack would re-render
-- the path from the layout template and the metadata, silently undoing the
-- rename -- and `unique_library_target` would then treat the operator's file as
-- somebody else's book and grow a ` (2)` suffix beside it. It is nullable
-- because the overwhelming majority of books never get renamed, and NULL means
-- exactly "render it from the template", which is what every pre-existing row
-- needs to keep doing.
ALTER TABLE artifacts ADD COLUMN library_relative_path TEXT;

-- Why a removal is recorded rather than just performed. A terminal
-- `download_jobs` row used to be permanent, and `list_history_jobs` documented
-- it as such, so deleting one silently would make the history lie about its own
-- completeness: an operator who removes forty rows and later wonders where they
-- went has nothing to read. The row survives the file being deleted, which is
-- also what lets the page distinguish「记录已删，文件还在」from「两者都删了」.
--
-- Not a column on `download_jobs`: the job row is what gets deleted, so a
-- column on it would vanish with the thing it was describing.
CREATE TABLE removed_works (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    title TEXT,
    -- The paths as they were at removal time, so the record still means
    -- something after the layout template changes.
    archive_path TEXT,
    cbz_path TEXT,
    -- 0 when only the bookkeeping was removed, 1 when the bytes went too. This
    -- is the field the operator is actually asking about later.
    deleted_files INTEGER NOT NULL DEFAULT 0 CHECK (deleted_files IN (0, 1)),
    operator_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_removed_works_candidate
    ON removed_works (candidate_id, id DESC);