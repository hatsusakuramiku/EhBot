-- Operator-editable system preferences.
--
-- A separate table rather than more rows in `archive_settings`: that table is
-- read by the archive and conversion services on every job, and its keys are
-- all about how a book is packed. Polling cadence, source concurrency and the
-- display timezone are none of those things, and putting them there would mean
-- the packing path loads settings it must never act on -- the same reason the
-- thumbnail cache is not a column on `candidates`.
--
-- Key/value rather than one column per preference for the opposite reason the
-- domain tables are typed: these are read as a mapping, written one form at a
-- time, and a new preference must not need a migration to land. Defaults live
-- in `app/settings/service.py`, so an absent row means "the default", not
-- "unset" -- there is no state where the interface has no cadence at all.
CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
