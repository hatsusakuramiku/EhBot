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
