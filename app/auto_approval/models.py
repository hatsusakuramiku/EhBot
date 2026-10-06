from __future__ import annotations

from dataclasses import dataclass

from app.review.models import REVIEW_APPROVE, REVIEW_REJECT


#: What a matching rule does to the candidate. The values are the review verbs
#: themselves rather than a parallel pair, so the rule's stored action and the
#: audit action it eventually writes can never drift into synonyms.
RULE_ACTION_APPROVE = REVIEW_APPROVE
RULE_ACTION_REJECT = REVIEW_REJECT
RULE_ACTIONS: tuple[str, ...] = (RULE_ACTION_APPROVE, RULE_ACTION_REJECT)


@dataclass(frozen=True, slots=True)
class AutoApprovalRule:
    rule_id: int
    name: str
    enabled: bool
    priority: int
    version: int
    condition: dict
    dsl_snapshot: str
    created_at: str
    updated_at: str
    #: Whether this rule's text and tag comparisons are case-sensitive. Off by
    #: default: matching is case-insensitive unless the operator turns it on.
    case_sensitive: bool = False
    #: `APPROVE` (approve and enqueue) or `REJECT` (reject). Defaulted to the
    #: old behaviour so every existing construction -- tests included -- keeps
    #: meaning what it meant before `action` existed.
    action: str = RULE_ACTION_APPROVE


@dataclass(frozen=True, slots=True)
class AutoApprovalMatch:
    rule: AutoApprovalRule
    metadata: dict[str, str]
    conditions: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class AutoApprovalDryRunHit:
    """One candidate a trial run matched, named well enough to recognise."""

    candidate_id: int
    title: str | None
    status: str


@dataclass(frozen=True, slots=True)
class AutoApprovalDryRun:
    """What a rule would have done to candidates already in the database.

    `scanned` is reported next to `matched` because the count alone cannot be
    read: 「命中 12」 means something very different over the last 20 works than
    over the last 500. `truncated` says whether older candidates exist beyond
    the window, so a run over a large history is not mistaken for a run over
    all of it.
    """

    scanned: int
    matched: int
    truncated: bool
    hits: tuple[AutoApprovalDryRunHit, ...]
    #: What the tried rule would do to the matches. Carried so the trial run can
    #: say 「将会自动驳回」 rather than describing every rule as if it approved.
    action: str = RULE_ACTION_APPROVE
