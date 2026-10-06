-- Automatic-approval rules gain an action: APPROVE (the existing behaviour)
-- or REJECT. Both kinds live in this one table and are evaluated in the same
-- priority order, so the first matching enabled rule decides regardless of its
-- action -- there is deliberately no second rule pool or second scan.
--
-- The default backfills every existing rule to APPROVE, so an upgraded database
-- keeps approving exactly what it approved before. The audit trail needs no
-- backfill: `review_actions.details_json` stores the whole rule snapshot per
-- automatic decision, and `action` is only read going forward.
ALTER TABLE auto_approval_rules
    ADD COLUMN action TEXT NOT NULL DEFAULT 'APPROVE'
    CHECK (action IN ('APPROVE', 'REJECT'));
