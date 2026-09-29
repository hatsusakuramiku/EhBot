-- Where the MTProto ingester resumes each source from.
--
-- The bot path needs no cursor: `getUpdates` hands over an offset, and the
-- unique key on `source_messages` already stops a message from being ingested
-- twice. A user account reads history instead, so it has to remember the last
-- message id it has looked at -- and that cannot be derived from
-- `source_messages` alone: a source that has never produced a candidate has no
-- rows there, and re-reading its whole archive on every poll is not an option.
ALTER TABLE telegram_sources ADD COLUMN last_message_id INTEGER;
