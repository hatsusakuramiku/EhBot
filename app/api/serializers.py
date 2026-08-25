"""DTO -> JSON mapping for `/api/v1`.

Serialisation lives here rather than inline in the route functions so a field
has one definition. Every state-bearing payload carries the resolved
``label``/``tone``/``live`` alongside the raw ``code``: the browser then renders
vocabulary without a second lookup, and a state can never appear translated on
one screen and raw on another.
"""

from __future__ import annotations

from typing import Any

from app.api.status import (
    connection_view,
    provider_label,
    status_view,
)
from app.review.models import field_label


def status_payload(code: str | None) -> dict[str, Any]:
    """Full status descriptor for a state code."""
    return status_view(code).to_payload()


def candidate_summary(item: Any) -> dict[str, Any]:
    """A candidate as it appears in a list or grid.

    Deliberately excludes messages and metadata history: this shape backs the
    candidate grid, where a page of 50 would otherwise pull hundreds of rows
    the view never renders.
    """
    return {
        "candidate_id": item.candidate_id,
        "status": status_payload(item.status),
        "filter_result": item.filter_result,
        "title": item.title,
        "artist": item.artist,
        "category": item.category,
        "language": item.language,
        "tags": _split_tags(item.tags),
        "raw_tags": _split_tags(item.raw_tags),
        "message_count": item.message_count,
        "updated_at": item.updated_at,
        "ex_gid": item.ex_gid,
        "ex_gallery_token": item.ex_gallery_token,
        # The detail page is the one destination for a candidate at any stage,
        # so the list hands the client the link instead of each caller
        # rebuilding it.
        "href": f"/works/{item.candidate_id}",
    }


def _split_tags(raw: str | None) -> list[str]:
    """Split a stored comma-joined tag string into a list.

    Tags are persisted as one delimited string; returning a list means the
    interface does not re-implement the split (and its whitespace handling)
    in JavaScript.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def metadata_entry(entry: Any) -> dict[str, Any]:
    """One metadata field value with its provenance.

    ``value_source`` and ``is_manual`` are exposed because the review UI has to
    show where a value came from -- an operator override must be visually
    distinct from a value ExHentai supplied.
    """
    return {
        "field_name": entry.field_name,
        "field_label": field_label(entry.field_name),
        "field_value": entry.field_value,
        "value_source": entry.value_source,
        "confidence": entry.confidence,
        "is_manual": entry.is_manual,
        "created_at": entry.created_at,
    }


def review_action(entry: Any) -> dict[str, Any]:
    """One audit-trail entry, for the detail page timeline."""
    return {
        "action": entry.action,
        "operator_name": entry.operator_name,
        "details": entry.details,
        "created_at": entry.created_at,
    }


def job_summary(job: Any) -> dict[str, Any]:
    """A download or packaging task as the activity view needs it.

    The computed properties (`progress_percent`, `is_retryable`, ...) are
    included rather than left to the client: they encode real policy, such as a
    stalled torrent not being a failure, and duplicating that logic in
    JavaScript is how the two would disagree.
    """
    return {
        "job_id": job.job_id,
        "candidate_id": job.candidate_id,
        "provider": {
            "code": job.provider,
            "label": provider_label(job.provider),
        },
        "state": status_payload(job.state),
        "attempt_count": job.attempt_count,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "artifact_path": job.artifact_path,
        "artifact_size": job.artifact_size,
        "artifact_cbz_path": job.artifact_cbz_path,
        "progress_percent": job.progress_percent,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "actions": {
            "retry": job.is_retryable,
            "pause": job.is_pausable,
            "resume": job.state == "PAUSED",
            "cancel": job.is_cancellable,
            "stop_seeding": job.is_seeding,
        },
        "torrent": {
            "waiting_for_peers": job.is_waiting_for_peers,
            "seeding": job.is_seeding,
            "already_in_client": job.was_already_in_client,
            "upload_speed": job.upload_speed,
            "state": job.torrent_state,
            # Minutes without progress. `None` means「not stalled」rather than
            # zero, so the interface can stay silent instead of showing「0 分钟」.
            "stalled_minutes": job.stalled_minutes,
        },
        "href": f"/works/{job.candidate_id}",
    }


def provider_connection(status: Any) -> dict[str, Any]:
    """One external connection's health."""
    view = connection_view(status.state)
    return {
        "state": view.to_payload(),
        "configured": status.configured,
        "identity": status.identity,
        "error": status.error,
    }


def connection_snapshot(snapshot: Any) -> dict[str, Any]:
    return {
        "telegram": provider_connection(snapshot.telegram),
        "exhentai": provider_connection(snapshot.exhentai),
    }


__all__ = [
    "candidate_summary",
    "connection_snapshot",
    "job_summary",
    "metadata_entry",
    "provider_connection",
    "review_action",
    "status_payload",
]
