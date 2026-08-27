-- Cover-art proxy cache, field locking and queue ordering.
--
-- `thumbnails` is a content-addressed cache, not a per-candidate column: the
-- same cover can back a candidate, and two galleries can legitimately share an
-- image. Keying by the digest of the source identity means the served URL is
-- stable forever, which is what lets the endpoint send `immutable` cache
-- headers honestly.
--
-- The library-shelf table this migration originally carried was dropped after
-- the product scope was clarified: EhBot manages download-to-archive only,
-- leaving book management to external tools. The CBZ output path is already
-- visible in the activity history via `artifact_cbz_path`.

-- The gdata `thumb` URL has been parsed since the metadata work landed but was
-- never stored. It goes on `candidates` rather than into `metadata_values` for
-- the same reason `preview_url` and `torrent_count` do: it is presentation and
-- routing state, and a view should not have to parse strings out of the
-- metadata table to render a grid.
ALTER TABLE candidates ADD COLUMN thumb_url TEXT;

-- Field locking. A manual value is already protected from re-scraping by the
-- `is_manual = 0` guard on the metadata upsert; this column covers the other
-- case an operator needs -- pinning a value that came from ExHentai so a later
-- scrape leaves it alone -- without having to fake an operator edit to do it.
ALTER TABLE metadata_values ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0
    CHECK (is_locked IN (0, 1));

-- Queue ordering. The default matches `archive_passwords.priority` so the two
-- read the same way: a lower number runs first, and 100 leaves room on both
-- sides for an operator to promote or demote without renumbering the queue.
ALTER TABLE download_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 100;

-- Page count for a packed CBZ. The conversion path already knew this number
-- but had nowhere to put it, so it was being written into `size_bytes` --
-- every packed CBZ reported a size of a few dozen bytes. The size column now
-- carries the real file size and the count lives here.
ALTER TABLE artifacts ADD COLUMN page_count INTEGER;

CREATE TABLE thumbnails (
    -- SHA-256 of the source identity *including* the variant, so each size is
    -- its own row and its own immutable URL. Never a digest of the rendered
    -- bytes: the URL has to be derivable before the image has been fetched.
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('CANDIDATE_COVER')),
    variant TEXT NOT NULL CHECK (variant IN ('card')),
    -- The cover the proxy fetches. `source_path` is unused today and kept
    -- nullable for a locally-rendered cover, should a later phase need one.
    source_url TEXT,
    source_path TEXT,
    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING', 'READY', 'FAILED')),
    content_type TEXT,
    byte_size INTEGER,
    width INTEGER,
    height INTEGER,
    error_code TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_thumbnails_state ON thumbnails (state, updated_at);