from __future__ import annotations

import asyncio
from app.auto_approval.models import (
    AutoApprovalDryRun,
    AutoApprovalDryRunHit,
    AutoApprovalMatch,
    AutoApprovalRule,
    RULE_ACTION_APPROVE,
)
from app.auto_approval.rules import RuleValidationError, evaluate_rule
from app.db.database import Database
from app.review.models import STATUS_PENDING_REVIEW


#: How far back a trial run reads. Bounded because the editor calls it while the
#: operator types: an unbounded scan over a year of history would evaluate every
#: rule against every candidate on each save, and the answer to 「这条规则会命中
#: 什么」 does not improve much past a few hundred recent works.
DRY_RUN_SCAN_LIMIT = 200

#: How many matched candidates are named back. Enough to recognise a rule that
#: is catching the wrong thing; not so many that the page becomes a queue view.
DRY_RUN_SAMPLE_LIMIT = 5


class AutomaticApprovalService:
    """Find the first enabled automatic-approval rule matching a candidate."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def matching_rule(
        self, candidate_id: int
    ) -> AutoApprovalMatch | None:
        return await asyncio.to_thread(self._matching_rule_sync, candidate_id)

    def _matching_rule_sync(self, candidate_id):
        with self._database.connection() as connection:
            connection.execute('BEGIN')
            return self.matching_rule_in_transaction(connection, candidate_id)

    def matching_rule_in_transaction(self, connection, candidate_id) -> AutoApprovalMatch | None:
        """Evaluate one coherent snapshot, also used inside the final write lock."""
        candidate = connection.execute('SELECT status, ex_gid FROM candidates WHERE id = ?', (candidate_id,)).fetchone()
        if candidate is None or candidate[0] != STATUS_PENDING_REVIEW:
            return None
        if candidate[1] is None:
            # A work with no gallery id cannot be recognised again, so a rule
            # that decides it is a decision the operator can never audit
            # against anything: the same book re-posted tomorrow is a new
            # candidate. Automatic approval is therefore gallery-linked only
            # (R59), and a no-id candidate waits for a human who has been told
            # why.
            return None
        metadata = self._database._effective_metadata_sync(candidate_id, connection)
        for rule in self._database._list_auto_approval_rules_sync(True, connection):
            try:
                result = evaluate_rule(
                    rule.condition, metadata, case_sensitive=rule.case_sensitive
                )
            except RuleValidationError:
                continue
            if result.matched:
                return AutoApprovalMatch(
                    rule=rule,
                    metadata=metadata,
                    conditions=result.conditions,
                    gallery_id=candidate[1],
                    # Integer candidate ids can be reused after purge. A late
                    # decision must still belong to the captured generation.
                    ingest_identity=tuple(connection.execute(
                        'SELECT id, generation, revision FROM ingest_entries WHERE candidate_id = ? ORDER BY id',
                        (candidate_id,),
                    ).fetchall()),
                )
        return None

    async def preview(self, rule: AutoApprovalRule) -> tuple[int, ...]:
        matched: list[int] = []
        # Gallery-linked only, the same population the sweep and the rule can
        # actually decide (R59); counting the rest would make the trial run
        # promise matches the automatic path is never allowed to take.
        for candidate_id in await self._database.pending_candidate_ids(
            require_gallery=True
        ):
            metadata = await self._database.effective_metadata(candidate_id)
            if evaluate_rule(
                rule.condition, metadata, case_sensitive=rule.case_sensitive
            ).matched:
                matched.append(candidate_id)
        return tuple(matched)

    async def dry_run(
        self,
        condition: dict,
        *,
        case_sensitive: bool = False,
        action: str = RULE_ACTION_APPROVE,
        scan_limit: int = DRY_RUN_SCAN_LIMIT,
        sample_limit: int = DRY_RUN_SAMPLE_LIMIT,
    ) -> AutoApprovalDryRun:
        """Report what a rule would match, without approving anything.

        `case_sensitive` is submitted with the form like the operators are: a
        trial run tests the unsaved rule as it stands in the editor, so its case
        setting is part of what gets tried. Takes a condition rather than a
        stored rule so the editor can try an unsaved one: the point of a trial
        run is to find out before saving. It reads candidates in every status,
        not just those pending review, because the question being asked is 「这条
        规则历史上会命中什么」 -- restricting the scan to the pending queue would
        answer it with whatever happens to be undecided this afternoon.

        Takes a condition rather than a stored rule so the editor can try an
        unsaved one: the point of a trial run is to find out before saving. It
        reads candidates in every status, not just those pending review, because
        the question being asked is 「这条规则历史上会命中什么」 -- restricting the
        scan to the pending queue would answer it with whatever happens to be
        undecided this afternoon.

        Nothing here writes. `evaluate_rule` is pure and the only database calls
        are reads, so a trial run over a rule that would approve fifty books
        approves none of them.
        """
        candidates, total = await self._database.list_candidates_page(
            limit=max(scan_limit, 0)
        )
        matched = 0
        hits: list[AutoApprovalDryRunHit] = []
        for item in candidates:
            metadata = await self._database.effective_metadata(item.candidate_id)
            try:
                result = evaluate_rule(condition, metadata, case_sensitive=case_sensitive)
            except RuleValidationError:
                # An unusable condition matches nothing rather than aborting the
                # run: the editor's own validation reports the syntax error, and
                # a half-finished rule should read as 「命中 0」, not as a 500.
                break
            if not result.matched:
                continue
            matched += 1
            if len(hits) < max(sample_limit, 0):
                hits.append(
                    AutoApprovalDryRunHit(
                        candidate_id=item.candidate_id,
                        title=item.title,
                        status=item.status,
                    )
                )
        return AutoApprovalDryRun(
            scanned=len(candidates),
            matched=matched,
            truncated=total > len(candidates),
            hits=tuple(hits),
            action=action,
        )


__all__ = [
    "DRY_RUN_SAMPLE_LIMIT",
    "DRY_RUN_SCAN_LIMIT",
    "AutomaticApprovalService",
]
