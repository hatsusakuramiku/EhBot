from __future__ import annotations

from app.auto_approval.models import AutoApprovalMatch, AutoApprovalRule
from app.auto_approval.rules import RuleValidationError, evaluate_rule
from app.db.database import Database
from app.review.models import STATUS_PENDING_REVIEW


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


__all__ = ["AutomaticApprovalService"]
