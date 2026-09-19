-- Archive-path routing rules: a condition in the auto-approval DSL pairs with a
-- layout template, so different works can land in different places inside the
-- library instead of every book following the one global `library_template`.
--
-- A mirror of `auto_approval_rules` (008 + 016's `case_sensitive`) with one
-- extra column: `path_template` is the layout a matching work uses. Rules are
-- evaluated in `priority, id` order at pack time; the first enabled rule whose
-- condition matches a work's effective metadata decides that work's template,
-- and a work no rule matches falls back to the global setting. A manual path
-- pin on the work always wins and never consults this table.
--
-- `dsl_snapshot` and `version` are kept for the same reason auto-approval keeps
-- them: a decision that records which rule it fired under has to be able to say
-- what that rule looked like.
CREATE TABLE archive_path_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    version INTEGER NOT NULL DEFAULT 1,
    condition_json TEXT NOT NULL,
    dsl_snapshot TEXT NOT NULL,
    path_template TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    case_sensitive INTEGER NOT NULL DEFAULT 0 CHECK (case_sensitive IN (0, 1))
);

CREATE INDEX idx_archive_path_rules_enabled_priority
    ON archive_path_rules (enabled, priority, id);