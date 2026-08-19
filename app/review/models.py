from __future__ import annotations

from dataclasses import dataclass


REVIEW_APPROVE = "APPROVE"
REVIEW_REJECT = "REJECT"
REVIEW_NEEDS_REVISION = "NEEDS_REVISION"
REVIEW_REQUEUE = "REQUEUE"
REVIEW_EDIT_METADATA = "EDIT_METADATA"

REVIEW_ACTIONS: tuple[str, ...] = (
    REVIEW_APPROVE,
    REVIEW_REJECT,
    REVIEW_NEEDS_REVISION,
    REVIEW_REQUEUE,
    REVIEW_EDIT_METADATA,
)

STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_NEEDS_INFO = "NEEDS_INFO"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_NEEDS_REVISION = "NEEDS_REVISION"
STATUS_PROCESSING = "PROCESSING"
STATUS_FAILED = "FAILED"

REVIEWABLE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PENDING_REVIEW,
        STATUS_NEEDS_INFO,
        STATUS_NEEDS_REVISION,
        STATUS_REJECTED,
    }
)

METADATA_FIELDS: tuple[str, ...] = (
    "Title",
    "Artist",
    "Language",
    "Category",
    "Tags",
    "Rating",
    "Description",
)


@dataclass(frozen=True, slots=True)
class MetadataEntry:
    field_name: str
    field_value: str
    value_source: str
    confidence: float | None
    is_manual: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class ReviewActionEntry:
    action: str
    operator_name: str
    details: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class CandidateReviewSummary:
    candidate_id: int
    title: str | None
    status: str
    metadata: tuple[MetadataEntry, ...]
    review_history: tuple[ReviewActionEntry, ...]
