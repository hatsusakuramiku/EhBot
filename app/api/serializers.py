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
    actor_view,
    attention_view,
    connection_view,
    metadata_source_view,
    provider_label,
    queue_group_view,
    review_action_view,
    row_note_view,
    status_view,
    toggle_view,
)
from app.review.models import (
    REVIEW_AUTO_APPROVE,
    REVIEW_LOCK_METADATA,
    REVIEWABLE_STATUSES,
    field_label,
)
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
        # Which review actions this candidate can still take, decided here
        # rather than in the template: the grid, the list and a JSON client all
        # need the same answer, and a page that re-derived it from the status
        # would be a second copy of `REVIEWABLE_STATUSES`.
        #
        # Downloadability is deliberately *not* folded in. It takes routing the
        # candidate's sources, which is a per-candidate read this shape exists
        # to avoid for a page of fifty; an unroutable approval fails loudly at
        # the point of action instead.
        "actions": {
            "approve": item.status in REVIEWABLE_STATUSES,
            "reject": item.status in REVIEWABLE_STATUSES,
        },
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

    ``source`` carries the resolved label beside the raw code for the same reason
    every other state does: the drawer renders provenance in JavaScript, and a
    lookup table there would be a second vocabulary to keep in step with
    `app/api/status.py`.
    """
    return {
        "field_name": entry.field_name,
        "field_label": field_label(entry.field_name),
        "field_value": entry.field_value,
        "value_source": entry.value_source,
        "source": metadata_source_view(entry.value_source).to_payload(),
        "confidence": entry.confidence,
        "is_manual": entry.is_manual,
        "is_locked": bool(getattr(entry, "is_locked", False)),
        "created_at": entry.created_at,
    }


def _review_reason(action: str, details: Any) -> str | None:
    """The one line explaining why an audit entry happened, or None.

    Composed here, beside `_torrent_detail`, for the same reason that one is: the
    timeline renders it in Jinja and the page's script re-renders it after a
    poll, so a format built twice would be two formats. The words are a sentence
    about a past event, not a state label -- states in this payload arrive as
    `StatusView`s from `app/api/status.py` and nothing here spells one out.

    An entry with nothing to explain returns None rather than an empty string:
    the timeline then omits the line instead of rendering a blank one.
    """
    if not isinstance(details, dict):
        return None
    explicit = details.get("note") or details.get("reason")
    if explicit:
        return str(explicit)
    if action == REVIEW_AUTO_APPROVE:
        name = details.get("rule_name")
        return f"命中规则「{name}」" if name else None
    field = details.get("field")
    if not field:
        return None
    if action == REVIEW_LOCK_METADATA:
        return f"{field_label(str(field))}{'已锁定' if details.get('locked') else '已解锁'}"
    value = details.get("value")
    if value in (None, ""):
        return field_label(str(field))
    return f"{field_label(str(field))}：{value}"


def review_action(entry: Any) -> dict[str, Any]:
    """One audit-trail entry, for the detail page timeline.

    The raw `action` and `operator_name` stay, because a client that stores or
    groups entries needs the codes; `action_view` and `actor` carry the resolved
    vocabulary beside them. `actor` is the answer to「谁决定的」-- an operator, a
    rule or the system -- which the stored name alone does not give, since it
    holds a login for a person and a reserved word otherwise.
    """
    return {
        "action": entry.action,
        "action_view": review_action_view(entry.action).to_payload(),
        "operator_name": entry.operator_name,
        "actor": actor_view(entry.operator_name).to_payload(),
        "reason": _review_reason(entry.action, entry.details),
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


def telegram_user_connection(account: Any) -> dict[str, Any]:
    """The MTProto account's health, plus the pending login's phone.

    `phone` is the number the operator typed on the login form, echoed so the
    verification step can name where the code went. Nothing else about the
    account leaves the server: the session string is a full account credential
    and never appears in a payload or a page.
    """
    view = connection_view(account.state)
    return {
        "state": view.to_payload(),
        "configured": account.configured,
        "identity": account.identity,
        "error": account.error,
        "phone": account.phone,
        # Resolved here rather than in the template: whether the login form is
        # asking for a code or for a password is a fact about the state, and a
        # template comparing raw state strings would be a second reader of the
        # vocabulary.
        "awaiting_code": account.state == "awaiting_code",
        "awaiting_password": account.state == "awaiting_password",
    }


def connection_snapshot(snapshot: Any) -> dict[str, Any]:
    return {
        "telegram": provider_connection(snapshot.telegram),
        "exhentai": provider_connection(snapshot.exhentai),
        "telegram_user": telegram_user_connection(snapshot.telegram_user),
    }


def telegram_source(source: Any) -> dict[str, Any]:
    """One configured Telegram source, filters included.

    Nothing here is a secret: the bot token lives in the credential store and a
    chat id is not one. The filter lists are returned as arrays rather than the
    comma-joined text the form submits, so a client never has to re-parse what
    the database already stores structured.

    `enabled` stays beside `enablement` because the two answer different
    questions: the form's checkbox needs the boolean, the row's badge needs the
    words, and having the words come from `toggle_view` is what keeps 「已停用」
    out of the template.
    """
    return {
        "source_id": source.source_id,
        "source_type": source.source_type,
        "chat_id": source.chat_id,
        "display_name": source.display_name,
        "enabled": source.enabled,
        "enablement": toggle_view(source.enabled).to_payload(),
        "allowed_archive_formats": list(source.allowed_archive_formats),
        "max_attachment_size_mb": source.max_attachment_size_mb,
        "required_tags": list(source.required_tags),
        "forbidden_tags": list(source.forbidden_tags),
        "allowed_languages": list(source.allowed_languages),
        "allowed_categories": list(source.allowed_categories),
        "min_rating": source.min_rating,
    }


def auto_approval_rule(rule: Any) -> dict[str, Any]:
    """One automatic-approval rule, with both its AST and its rendered text.

    Both forms travel: the editor rebuilds its condition groups from `condition`,
    while `dsl` is the non-executable rendering an operator reads to confirm the
    rule says what they meant. Rendering it in the browser instead would put a
    second DSL writer next to `render_rule_dsl`.
    """
    return {
        "rule_id": rule.rule_id,
        "name": rule.name,
        "enabled": rule.enabled,
        "enablement": toggle_view(rule.enabled).to_payload(),
        "priority": rule.priority,
        "version": rule.version,
        "condition": rule.condition,
        "dsl": rule.dsl_snapshot,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def auto_approval_dry_run(result: Any) -> dict[str, Any]:
    """What a trial run found, counts beside the sample it names."""
    return {
        "scanned": result.scanned,
        "matched": result.matched,
        "truncated": result.truncated,
        "hits": [
            {
                "candidate_id": hit.candidate_id,
                "title": hit.title,
                "status": status_payload(hit.status),
                "href": f"/works/{hit.candidate_id}",
            }
            for hit in result.hits
        ],
    }


def archive_password(entry: Any) -> dict[str, Any]:
    """A vault entry as the page may see it -- never the password itself.

    `last_success_at` is the field that makes the list useful: it is how an
    operator tells a password that is still earning its place from one that has
    not opened anything since it was added.
    """
    return {
        "password_id": entry.password_id,
        "name": entry.name,
        "priority": entry.priority,
        "enabled": entry.enabled,
        "enablement": toggle_view(entry.enabled).to_payload(),
        "last_success_at": entry.last_success_at,
        "created_at": entry.created_at,
    }


def tool_profile(profile: Any) -> dict[str, Any]:
    """One registered extraction tool. Operators never submit raw commands."""
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "backend": profile.backend,
        "kind": profile.kind,
        "executable_path": profile.executable_path,
        "supported_formats": list(profile.supported_formats),
        "timeout_seconds": profile.timeout_seconds,
        "capabilities": list(profile.capabilities),
        "enabled": profile.enabled,
        "enablement": toggle_view(profile.enabled).to_payload(),
    }


def safety_limits(limits: Any) -> dict[str, Any]:
    """The pre-extraction ceilings, keyed as the form fields that write them."""
    return {
        "max_members": limits.max_members,
        "max_total_bytes": limits.max_total_bytes,
        "max_member_bytes": limits.max_member_bytes,
        "max_compression_ratio": limits.max_compression_ratio,
        "max_depth": limits.max_depth,
    }


__all__ = [
    "archive_password",
    "auto_approval_dry_run",
    "auto_approval_rule",
    "candidate_summary",
    "connection_snapshot",
    "job_summary",
    "metadata_entry",
    "provider_connection",
    "queue_group_payload",
    "review_action",
    "safety_limits",
    "status_payload",
    "telegram_source",
    "telegram_user_connection",
    "tool_profile",
]
