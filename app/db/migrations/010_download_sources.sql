ALTER TABLE candidates ADD COLUMN preview_url TEXT;
ALTER TABLE candidates ADD COLUMN torrent_count INTEGER;
ALTER TABLE candidates ADD COLUMN torrent_hash TEXT;
ALTER TABLE source_messages ADD COLUMN preview_urls_json TEXT NOT NULL DEFAULT '[]';
