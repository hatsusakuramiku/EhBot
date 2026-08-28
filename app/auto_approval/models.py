from __future__ import annotations

from dataclasses import dataclass


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
