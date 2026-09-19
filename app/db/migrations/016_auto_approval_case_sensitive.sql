-- Automatic-approval rules move from a mixed regex/SQL-like DSL to a pure
-- SQL-like one (in / not in / like / not like / = / <> / > / < / >= / <= /
-- Exists / not Exists), and every comparison becomes case-insensitive by
-- default with a per-rule toggle.
--
-- The column: `case_sensitive` answers whether text and tag comparisons in this
-- rule case-fold. It sits on the rule, not the condition, because the operator
-- chose a per-rule toggle and the DSL offers no place for it.
ALTER TABLE auto_approval_rules
    ADD COLUMN case_sensitive INTEGER NOT NULL DEFAULT 0
    CHECK (case_sensitive IN (0, 1));

-- The operator set that exists in this database was replaced wholesale: regex,
-- CONTAINS, STARTS_WITH, HAS / HAS_ANY / HAS_ALL and != no longer exist, and
-- none of them translate one-for-one (a general regex is not expressible as
-- LIKE). Rather than leave every stored rule silently matching nothing, the
-- schema reset clears them -- a deliberate, operator-approved rebuild (2026).
-- The audit trail is untouched: `review_actions` keeps the `dsl_snapshot` and
-- version each approval fired as, so removing the rule rows does not rewrite
-- history.
DELETE FROM auto_approval_rules;