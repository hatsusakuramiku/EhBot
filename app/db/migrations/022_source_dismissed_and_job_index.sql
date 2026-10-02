-- 来源删除的墓碑，以及按候选查任务的索引。
--
-- `telegram_sources` is not only operator-created. `discover_telegram_source`
-- inserts a row for every chat a message ever arrives from, so a plain DELETE
-- does not stick: the next message from that chat re-creates the row (disabled).
-- R49 recorded that as the reason 批量建来源 was refused -- a row you cannot get
-- rid of is a trap. `dismissed` makes the delete real: a dismissed row stays
-- dismissed through discovery, is hidden from the settings list, and is only
-- revived when the operator saves that source again on purpose.
--
-- `idx_download_jobs_candidate` backs the two paths that act on *all* of one
-- work's jobs rather than just the newest (the corrected 移除 and the new 彻底
-- 删除): a candidate may hold one row per source it was tried with, and the old
-- query plan scanned for them.
ALTER TABLE telegram_sources
    ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0
    CHECK (dismissed IN (0, 1));

CREATE INDEX idx_telegram_sources_dismissed
    ON telegram_sources (dismissed, enabled, id);

CREATE INDEX idx_download_jobs_candidate
    ON download_jobs (candidate_id);
