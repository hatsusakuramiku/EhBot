from __future__ import annotations

from app.auto_approval.models import (
    AutoApprovalDryRun,
    AutoApprovalDryRunHit,
    AutoApprovalMatch,
    AutoApprovalRule,
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
        candidate = await self._database.get_candidate(candidate_id)
        if candidate is None or candidate.status != STATUS_PENDING_REVIEW:
            return None
        metadata = await self._database.effective_metadata(candidate_id)
        for rule in await self._database.list_auto_approval_rules(enabled_only=True):
            try:
                result = evaluate_rule(rule.condition, metadata)
            except RuleValidationError:
                continue
            if result.matched:
                return AutoApprovalMatch(
                    rule=rule,
                    metadata=metadata,
                    conditions=result.conditions,
                )
        return None

    async def preview(self, rule: AutoApprovalRule) -> tuple[int, ...]:
        matched: list[int] = []
        for candidate_id in await self._database.pending_candidate_ids():
            metadata = await self._database.effective_metadata(candidate_id)
            if evaluate_rule(rule.condition, metadata).matched:
                matched.append(candidate_id)
        return tuple(matched)

    async def dry_run(
        self,
        condition: dict,
        *,
        scan_limit: int = DRY_RUN_SCAN_LIMIT,
        sample_limit: int = DRY_RUN_SAMPLE_LIMIT,
    ) -> AutoApprovalDryRun:
        """Report what a rule would match, without approving anything.

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
                result = evaluate_rule(condition, metadata)
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
        )


__all__ = [
    "DRY_RUN_SAMPLE_LIMIT",
    "DRY_RUN_SCAN_LIMIT",
    "AutomaticApprovalService",
]
