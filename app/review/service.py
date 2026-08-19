from __future__ import annotations

import asyncio
import json
import sqlite3

from app.db.database import Database
from app.review.models import (
    METADATA_FIELDS,
    REVIEW_APPROVE,
    REVIEW_EDIT_METADATA,
    REVIEW_NEEDS_REVISION,
    REVIEW_REJECT,
    REVIEW_REQUEUE,
    REVIEWABLE_STATUSES,
    STATUS_APPROVED,
    STATUS_NEEDS_REVISION,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    CandidateReviewSummary,
    MetadataEntry,
    ReviewActionEntry,
)


class ReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ReviewService:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def approve_candidate(
        self, candidate_id: int, operator_name: str, note: str | None = None
    ) -> None:
        await self._apply_status_transition(
            candidate_id,
            operator_name,
            REVIEW_APPROVE,
            STATUS_APPROVED,
            note,
        )

    async def reject_candidate(
        self, candidate_id: int, operator_name: str, reason: str
    ) -> None:
        reason = reason.strip()
        if not reason:
            raise ReviewError(
                "REVIEW_REASON_REQUIRED",
                "Reject reason is required",
            )
        await self._apply_status_transition(
            candidate_id,
            operator_name,
            REVIEW_REJECT,
            STATUS_REJECTED,
            reason,
        )

    async def request_revision(
        self, candidate_id: int, operator_name: str, reason: str
    ) -> None:
        reason = reason.strip()
        if not reason:
            raise ReviewError(
                "REVISION_REASON_REQUIRED",
                "Revision reason is required",
            )
        await self._apply_status_transition(
            candidate_id,
            operator_name,
            REVIEW_NEEDS_REVISION,
            STATUS_NEEDS_REVISION,
            reason,
        )

    async def requeue_candidate(
        self, candidate_id: int, operator_name: str, note: str | None = None
    ) -> None:
        await self._apply_status_transition(
            candidate_id,
            operator_name,
            REVIEW_REQUEUE,
            STATUS_PENDING_REVIEW,
            note,
        )

    async def set_manual_metadata(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        field_value: str,
    ) -> None:
        if field_name not in METADATA_FIELDS:
            raise ReviewError(
                "METADATA_FIELD_INVALID",
                f"Unsupported metadata field: {field_name}",
            )
        cleaned_value = field_value.strip()
        if not cleaned_value:
            raise ReviewError(
                "METADATA_VALUE_REQUIRED",
                "Metadata value is required",
            )
        if field_name == "Rating":
            try:
                float(cleaned_value)
            except ValueError as exc:
                raise ReviewError(
                    "METADATA_VALUE_INVALID",
                    "Rating must be a number",
                ) from exc
        await self._database.set_manual_metadata(
            candidate_id, operator_name, field_name, cleaned_value
        )

    async def get_candidate_review_summary(
        self, candidate_id: int
    ) -> CandidateReviewSummary | None:
        return await asyncio.to_thread(
            self._build_summary_sync, candidate_id
        )

    async def list_review_actions(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        return await self._database.list_review_actions(candidate_id)

    async def list_metadata(
        self, candidate_id: int
    ) -> tuple[MetadataEntry, ...]:
        return await self._database.list_metadata(candidate_id)

    async def _apply_status_transition(
        self,
        candidate_id: int,
        operator_name: str,
        action: str,
        new_status: str,
        note: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._validate_transition_sync,
            candidate_id,
            operator_name,
            action,
            new_status,
            note,
        )

    def _validate_transition_sync(
        self,
        candidate_id: int,
        operator_name: str,
        action: str,
        new_status: str,
        note: str | None,
    ) -> None:
        try:
            self._database._transition_candidate_status_sync(
                candidate_id,
                operator_name,
                action,
                new_status,
                note,
            )
        except ReviewError:
            raise
        except (LookupError, PermissionError) as exc:
            raise ReviewError(
                "REVIEW_INVALID_TRANSITION", str(exc)
            ) from exc
        except sqlite3.Error as exc:
            raise ReviewError(
                "REVIEW_DATABASE_ERROR", str(exc)
            ) from exc

    def _build_summary_sync(
        self, candidate_id: int
    ) -> CandidateReviewSummary | None:
        detail = self._database._get_candidate_sync(candidate_id)
        if detail is None:
            return None
        metadata = self._database.list_metadata_sync(candidate_id)
        history = self._database.list_review_actions_sync(candidate_id)
        return CandidateReviewSummary(
            candidate_id=detail.candidate_id,
            status=detail.status,
            title=detail.title,
            metadata=metadata,
            review_history=history,
        )


__all__ = ["ReviewError", "ReviewService"]
