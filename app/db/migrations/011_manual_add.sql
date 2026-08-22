-- Manual add-task from the operator: an ExHentai gallery link or a bare
-- magnet link is turned straight into an approved candidate. A magnet has no
-- gallery reference, so it is recorded by btih (in `torrent_hash`) plus the
-- original link; the EH_TORRENT provider pushes it with the magnet URL rather
-- than fetching a `.torrent`.
ALTER TABLE candidates ADD COLUMN magnet_url TEXT;