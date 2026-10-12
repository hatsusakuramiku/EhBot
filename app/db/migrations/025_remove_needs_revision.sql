-- 「要求修订」 is removed entirely (R84). Two data repairs, no schema change:
-- `candidates.status` is bare TEXT with no CHECK constraint (001_initial), so a
-- row carrying the retired state is fixed in place rather than by rebuilding the
-- table.
--
-- 1. A work parked at `NEEDS_REVISION` becomes `NEEDS_INFO`: that is the
--    closest surviving state, it stays in the review queue, and the operator can
--    approve, reject or requeue it exactly as before.
-- 2. The audit trail is erased too -- the operator asked for 「完全移除，一点都不要
--    留」, which includes the timeline entries that verb wrote. `review_actions`
--    rows for every other verb are untouched.
--
-- Both statements are idempotent: a second run matches nothing.
UPDATE candidates SET status = 'NEEDS_INFO' WHERE status = 'NEEDS_REVISION';
DELETE FROM review_actions WHERE action = 'NEEDS_REVISION';
