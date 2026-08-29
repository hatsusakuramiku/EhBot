-- Explicit archive paths: where the operator says one book must live.
--
-- Why a table rather than the column 014 added. `artifacts.library_relative_path`
-- could only exist once a CBZ did, because an artifact row is created *by* a
-- packing run. That made the pin unable to answer the question the work detail
-- page now asks: 「下次打包时把这本书放到哪里」, which an operator wants to set
-- *before* the first pack as readily as after it. Worse, the pin died with the
-- artifact -- removing a work's records dropped the operator's decision about
-- where its book belongs, so a re-download re-derived the path from the template
-- and undid a rename nobody had cancelled.
--
-- Keyed by candidate because that is the identity of a work across every job it
-- ever has: two downloads and three repacks are one book, and the path is a fact
-- about the book. `artifacts.library_relative_path` stays where it is and stays
-- accurate for rows written before this migration -- `ConversionService` reads
-- this table first and falls back to it, so an upgrade keeps every existing
-- rename working without a data migration that would have to guess.
CREATE TABLE work_archive_paths (
    candidate_id INTEGER PRIMARY KEY
        REFERENCES candidates(id) ON DELETE CASCADE,
    -- Library-relative, forward slashes, always ending in `.cbz`. Relative
    -- rather than absolute so moving the library directory carries every pinned
    -- book with it, and re-validated on read regardless: a path joined onto a
    -- root is the shape that must not be trusted twice.
    relative_path TEXT NOT NULL,
    -- 1 when an operator typed this path, 0 when the batch re-file computed it
    -- from the layout template. The distinction is the same one `is_manual`
    -- draws over metadata: a template re-render may overwrite a computed path,
    -- and must never overwrite one the operator chose. Without it, the first
    -- 批量打包 after a rename would quietly undo the rename for every selected
    -- book.
    is_manual INTEGER NOT NULL DEFAULT 1 CHECK (is_manual IN (0, 1)),
    operator_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The uniqueness a rename has to check before it moves anything. Two books
-- pinned to one path would race for the same file and the loser would be
-- overwritten with no trace, which is exactly what `unique_library_target`'s
-- ` (2)` suffix exists to prevent for *rendered* paths. An explicit path gets
-- no suffix -- an operator who typed a name that is taken is told so and
-- nothing moves -- so the guard has to be here.
CREATE UNIQUE INDEX idx_work_archive_paths_relative
    ON work_archive_paths (relative_path);