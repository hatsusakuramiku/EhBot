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
    NOTE_SEEDING,
    attention_view,
    connection_view,
    provider_label,
    queue_group_view,
    row_note_view,
    status_view,
)
from app.review.models import field_label
from app.thumbnails import THUMBNAIL_VARIANT_CARD, identity_hash


def status_payload(code: str | None) -> dict[str, Any]:
    """Full status descriptor for a state code."""
    return status_view(code).to_payload()


def _attention_payload(reason: str | None) -> dict[str, Any] | None:
    """What the operator has to do about a job, or null if nothing."""
    view = attention_view(reason)
    return view.to_payload() if view is not None else None


def queue_group_payload(group: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """One queue section with its heading and its own rows.

    The count comes from the section's own list rather than being passed in, so
    a heading cannot claim a number the rows below it do not add up to.
    """
    view = queue_group_view(group)
    return {
        "group": view.to_payload(),
        "count": len(jobs),
        "jobs": jobs,
    }


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
        "cover": _cover(getattr(item, "thumb_url", None)),
        # The detail page is the one destination for a candidate at any stage,
        # so the list hands the client the link instead of each caller
        # rebuilding it.
        "href": f"/works/{item.candidate_id}",
    }


def _cover(thumb_url: str | None) -> dict[str, Any] | None:
    """The proxied cover URL for a candidate, or ``None`` when there is none.

    The upstream URL is *not* returned. Pointing an ``<img>`` at ExHentai's CDN
    would tell that host every cover this deployment renders and would break
    the moment they refuse the hotlink; the digest is derived here so the
    client only ever talks to us. It is derivable without a fetch because the
    hash covers the source identity, not the rendered bytes.
    """
    cleaned = (thumb_url or "").strip()
    if not cleaned:
        return None
    digest = identity_hash(cleaned, THUMBNAIL_VARIANT_CARD)
    return {
        "url": f"/api/v1/thumbnails/{digest}",
        "hash": digest,
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
    distinct from a value ExHentai supplied. ``is_locked`` is the third state:
    a value that came from ExHentai but which the operator has pinned, so a
    re-scrape leaves it alone.
    """
    return {
        "field_name": entry.field_name,
        "field_label": field_label(entry.field_name),
        "field_value": entry.field_value,
        "value_source": entry.value_source,
        "confidence": entry.confidence,
        "is_manual": entry.is_manual,
        "is_locked": bool(getattr(entry, "is_locked", False)),
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


def _kib(value: int) -> str:
    """A byte rate as whole KiB/s. Fractions of a KiB are noise at this size."""
    return f"{int(value) // 1024} KiB/s"


def _torrent_detail(job: Any) -> str | None:
    """The one-line transfer summary, or ``None`` when there is nothing to say.

    Composed here rather than in the template because the activity page patches
    this text in place on every poll: built in Jinja *and* in JavaScript it would
    be two formats that drift, which is the duplication this refactor removes.
    It carries no state label -- those come from ``status_view`` -- only numbers
    and their units.
    """
    parts: list[str] = []
    if job.is_waiting_for_peers:
        parts.append(f"{job.progress_percent}%")
        parts.append(f"做种者 {job.num_seeds}")
        if job.download_speed:
            parts.append(f"↓{_kib(job.download_speed)}")
        if job.eta_seconds is not None:
            parts.append(f"剩 {job.eta_seconds // 60} 分")
    elif job.is_seeding:
        parts.append(f"↑{_kib(job.upload_speed)}")
    if not parts:
        return None
    return " · ".join(parts)


def _note_payload(job: Any) -> dict[str, Any] | None:
    """The row's extra badge, or ``None`` when its state says everything.

    Only seeding qualifies today. It is resolved here rather than in the
    template because a template that writes「正在做种」itself is a state label
    outside `app.api.status`, which is how the old page ended up saying
    「下载完成，正在做种」in one place and nothing at all in another.
    """
    if not job.is_seeding:
        return None
    view = row_note_view(NOTE_SEEDING)
    return view.to_payload() if view is not None else None


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
        # Which of the four queue sections the row belongs in, and why the
        # operator is needed if they are. Decided in Python because「停滞的种子
        # 不是失败」is policy, and a client that regrouped rows itself would be
        # free to disagree with the count in the group heading.
        "group": job.queue_group,
        "attention": _attention_payload(job.attention_reason),
        # What the row is doing beyond its lifecycle state -- today only「正在
        # 做种」, which `COMPLETED` alone would hide.
        "note": _note_payload(job),
        "priority": job.priority,
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
            "download_speed": job.download_speed,
            "num_seeds": job.num_seeds,
            "eta_seconds": job.eta_seconds,
            "state": job.torrent_state,
            # Minutes without progress. `None` means「not stalled」rather than
            # zero, so the interface can stay silent instead of showing「0 分钟」.
            "stalled_minutes": job.stalled_minutes,
            # The rendered line the queue shows under the job id. `None` when
            # the job is not a live transfer, so the row has nothing to print.
            "detail": _torrent_detail(job),
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
    "queue_group_payload",
    "review_action",
    "status_payload",
]
