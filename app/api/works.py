"""Unified work detail: one snapshot for one work, at any stage.

A candidate, an in-flight download and a packaged book are the same work at
three points of its life, so there is one detail payload and one page for all of
them rather than a shape and a layout per stage. `work_snapshot` is the single
assembler: `GET /api/v1/works/{id}` returns it and `GET /works/{id}` renders the
same dict, which is what stops the page and the API from disagreeing about what
a work can still do.

Three things are computed here rather than left to whoever renders it:

* **The stage** -- derived from the candidate's status and its jobs, never
  stored. A stored stage would be a fourth column to keep in step with the two
  that already answer the question.
* **What the work can still do** (`actions`) -- policy, and the page, the JSON
  client and the eventual keyboard shortcut all need the same answer. A template
  that re-derived it from the status would be a second copy of
  `REVIEWABLE_STATUSES`.
* **The timeline** -- the audit trail and the task history merged into one
  ordered list. They are one story: a rule approving a candidate and the
  download it created are cause and effect, and two lists side by side make the
  operator interleave them by reading timestamps.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError
from app.api.serializers import (
    job_summary,
    metadata_entry,
    review_action,
    status_payload,
)
from app.api.status import (
    STAGE_ARCHIVED,
    STAGE_CANDIDATE,
    STAGE_DOWNLOAD,
    attachment_kind_view,
    is_live,
    work_stage_view,
)
from app.downloads.models import (
    PROVIDER_CONVERSION,
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
    PROVIDER_TELEGRAM_USER,
    PROVIDER_TELEGRAPH,
)
from app.review.models import REVIEWABLE_STATUSES, split_metadata_entries


router = APIRouter(tags=["works"])


#: Statuses that mean the pipeline has taken the work on. `DOWNLOADED` is here
#: rather than under 入库期 because a downloaded archive is not a book yet -- it
#: is packaging's input, and the operator's next action is still a task action.
_DOWNLOAD_STATUSES: frozenset[str] = frozenset(
    {"APPROVED", "PROCESSING", "DOWNLOADED", "FAILED"}
)


#: The form path that runs each source, by provider. It lives beside the
#: availability rule rather than in the template because the page and the JSON
#: client have to agree about which route a source is fetched from, and a mapping
#: written in Jinja would be a second table to keep in step with `app/main.py`.
#: `PROVIDER_CONVERSION` is absent for the reason it is absent from
#: `SUPPORTED_PROVIDERS`: packaging is not a source an operator picks.
_SOURCE_ROUTES: dict[str, str] = {
    PROVIDER_TELEGRAM: "download",
    PROVIDER_TELEGRAM_USER: "telegram-user",
    PROVIDER_EXHENTAI: "exhentai-archive",
    PROVIDER_TELEGRAPH: "telegraph",
    PROVIDER_EH_TORRENT: "torrent",
}


def _packaged_job(jobs) -> Any | None:
    """The finished packaging task, or None when nothing has been packaged.

    Read from the job's own CBZ artifact rather than from the candidate's status:
    packaging leaves the candidate in whatever state the download left it, so the
    artifact is the only thing that actually says「这本已经打好包了」.
    """
    for job in jobs:
        if job.provider == PROVIDER_CONVERSION and job.artifact_cbz_path:
            return job
    return None


def work_stage(status: str | None, jobs) -> str:
    """Which of the three stages the work is in.

    Ordered most-advanced first: a packaged work stays 入库期 even after a
    re-download puts a new task in flight, because the book on disk is what the
    operator came to the page for. Anything the review flow can still act on is
    候选期, and everything else -- a work the pipeline has taken on, or one with
    an open task -- is 下载期.
    """
    if _packaged_job(jobs) is not None:
        return STAGE_ARCHIVED
    if status in REVIEWABLE_STATUSES:
        return STAGE_CANDIDATE
    if status in _DOWNLOAD_STATUSES or jobs:
        return STAGE_DOWNLOAD
    return STAGE_CANDIDATE


def work_actions(candidate, jobs, sources: frozenset[str]) -> dict[str, Any]:
    """What this work can still do, per stage.

    `sources` is the set of providers this deployment can actually reach -- a
    button for an unconfigured qBittorrent is a button that can only fail, and
    the page has no way to know what is configured. ExHentai Archive Download is
    offered whenever the gallery is known and the account is configured, and
    never routed automatically anywhere: it spends GP, which stays an explicit
    decision.
    """
    reviewable = candidate.status in REVIEWABLE_STATUSES
    # Any provider that finished leaves an ARCHIVE artifact, and that is all
    # packaging needs as input. Testing for TELEGRAM specifically used to hide
    # the button for every other source.
    archive_ready = any(
        job.state == "COMPLETED" and job.artifact_path for job in jobs
    )
    packaged = _packaged_job(jobs)
    return {
        "approve": reviewable,
        "reject": reviewable,
        "needs_revision": reviewable,
        # Requeueing is what un-parks a work the operator or a rule set aside.
        # Offered only where it changes something: a PENDING_REVIEW candidate is
        # already in the queue it would move to.
        "requeue": candidate.status in {"REJECTED", "NEEDS_REVISION"},
        # Metadata is editable at every stage on purpose: a wrong title found
        # after packaging is exactly when an operator wants to fix it, and the
        # fix is what the next re-package will read.
        "edit_metadata": True,
        "fetch_metadata": bool(candidate.ex_gid) and PROVIDER_EXHENTAI in sources,
        "convert": archive_ready and packaged is None,
        "reconvert": packaged is not None or archive_ready,
        "sources": _source_actions(candidate, sources),
    }


def _has_archive(candidate) -> bool:
    """Whether any source message carried an archive attachment."""
    return any(
        attachment.get("type") == "archive"
        for message in candidate.messages
        for attachment in message.attachments
    )


def _source_actions(candidate, sources: frozenset[str]) -> list[dict[str, Any]]:
    """The routes an operator can take the original archive from.

    Each entry says whether it is available and, when it is not, why -- an
    absent button leaves an operator wondering whether the source is missing or
    the page is broken, and a disabled one with a reason answers that. The
    reasons are about *this* work (no torrent hash, no gallery, no preview page),
    not about a state, so they are page copy rather than vocabulary.
    """
    entries: list[tuple[str, bool, str | None]] = [
        (
            PROVIDER_EH_TORRENT,
            bool(candidate.torrent_hash) and PROVIDER_EH_TORRENT in sources,
            None
            if candidate.torrent_hash
            else ("画廊没有可用种子" if candidate.torrent_count == 0 else "尚未拉取种子信息"),
        ),
        (
            PROVIDER_EXHENTAI,
            bool(candidate.ex_gid) and PROVIDER_EXHENTAI in sources,
            None if candidate.ex_gid else "没有关联画廊",
        ),
        (
            PROVIDER_TELEGRAPH,
            bool(candidate.preview_url) and PROVIDER_TELEGRAPH in sources,
            None if candidate.preview_url else "没有预览页",
        ),
        (
            PROVIDER_TELEGRAM,
            _has_archive(candidate),
            None,
        ),
        (
            # Offered whenever there is an attachment *and* an account, at any
            # size: the operator reaching for this button is usually the one whose
            # 20 MB attempt just failed, and hiding it under a size test would
            # make the recovery path invisible on exactly those works.
            PROVIDER_TELEGRAM_USER,
            _has_archive(candidate) and PROVIDER_TELEGRAM_USER in sources,
            None
            if _has_archive(candidate)
            else "来源消息没有压缩附件",
        ),
    ]
    return [
        {
            "provider": status_payload(provider),
            "available": available,
            "hint": hint,
            "action": (
                f"/candidates/{candidate.candidate_id}/{_SOURCE_ROUTES[provider]}"
            ),
        }
        for provider, available, hint in entries
    ]


def work_timeline(actions, jobs) -> list[dict[str, Any]]:
    """The audit trail and the task history as one list, newest first.

    Merged rather than shown side by side because they are one story told in two
    tables: a rule that approved a candidate and the download it created are
    cause and effect, and an operator reading two lists has to interleave them
    by timestamp themselves.

    A job contributes one node carrying its current state, not one per
    transition -- the table keeps no transition history, and inventing nodes for
    states a job passed through would be a timeline that claims to know more
    than the database does. Its node also carries the job's own actions, so a
    failed task is retried where the operator finds out it failed.

    Ties keep review entries after the job they belong to: an approval and the
    job it enqueued land in the same second, and Python's sort is stable, so the
    build order below is what settles them.
    """
    nodes: list[dict[str, Any]] = []
    for job in jobs:
        payload = job_summary(job)
        nodes.append(
            {
                "kind": "JOB",
                "at": job.created_at,
                "job": payload,
                "state": payload["state"],
                "provider": payload["provider"],
                # A job's own reason to exist on the timeline is what went wrong
                # with it; a healthy task says everything through its state.
                "reason": job.error_message,
                "actions": {
                    "retry": job.is_retryable,
                    "pause": job.is_pausable,
                    "resume": job.state == "PAUSED",
                    "cancel": job.is_cancellable,
                },
            }
        )
    for entry in actions:
        payload = review_action(entry)
        nodes.append(
            {
                "kind": "REVIEW",
                "at": payload["created_at"],
                "action": payload["action_view"],
                "actor": payload["actor"],
                "operator_name": payload["operator_name"],
                "reason": payload["reason"],
                "details": payload["details"],
            }
        )
    nodes.sort(key=lambda node: node["at"] or "", reverse=True)
    return nodes


def _attachment_payload(attachment: dict[str, Any]) -> dict[str, Any]:
    """One source-message attachment, with its kind already resolved to words.

    The kind is resolved here rather than on the page because a photo and an
    archive want different things said about them -- a photo has no filename an
    operator would recognise -- and the page must not be the place where that
    decision lives.
    """
    kind = attachment.get("type")
    payload = dict(attachment)
    payload["kind"] = attachment_kind_view(kind).to_payload()
    return payload


async def work_snapshot(
    database,
    candidate_id: int,
    *,
    download=None,
    sources: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Everything one work's detail page needs, in one read.

    Returns None when the candidate does not exist, leaving the caller to decide
    between a JSON error and an HTML 404 -- the two are the same fact reported in
    two grammars, and raising here would force one of them.
    """
    candidate = await database.get_candidate(candidate_id)
    if candidate is None:
        return None

    entries = await database.list_metadata(candidate_id)
    # Translated values and upstream originals are split server-side using the
    # same helper the old page used, so the two never interleave into a list
    # showing every field twice.
    primary, raw = split_metadata_entries(entries)
    actions = await database.list_review_actions(candidate_id)
    jobs = (
        await download.list_jobs_for_candidate(candidate_id)
        if download is not None
        else ()
    )
    stage = work_stage(candidate.status, jobs)
    packaged = _packaged_job(jobs)

    return {
        "candidate_id": candidate.candidate_id,
        "status": status_payload(candidate.status),
        "stage": work_stage_view(stage).to_payload(),
        "title": candidate.title,
        "filter_result": candidate.filter_result,
        "filter_reason": candidate.filter_reason,
        "ex_gid": candidate.ex_gid,
        "ex_gallery_token": candidate.ex_gallery_token,
        "preview_url": candidate.preview_url,
        # `None` means gdata has not answered yet, which is a different thing
        # from a gallery genuinely having no torrent; the interface needs to
        # tell「未查询」from「确认无种」.
        "torrent_count": candidate.torrent_count,
        "torrent_hash": candidate.torrent_hash,
        "messages": [
            {
                "chat_title": message.chat_title,
                "message_id": message.message_id,
                "message_text": message.message_text,
                "attachments": [
                    _attachment_payload(attachment)
                    for attachment in message.attachments
                ],
                "message_date": message.message_date,
            }
            for message in candidate.messages
        ],
        "metadata": [metadata_entry(entry) for entry in primary],
        "raw_metadata": [metadata_entry(entry) for entry in raw],
        "timeline": work_timeline(actions, jobs),
        "jobs": [job_summary(job) for job in jobs],
        # The packaged output, promoted out of the job list: it is what the work
        # became, and a page should not have to scan tasks to find the book.
        "archive": (
            {
                "path": packaged.artifact_cbz_path,
                "job_id": packaged.job_id,
                "completed_at": packaged.updated_at,
            }
            if packaged is not None
            else None
        ),
        "actions": work_actions(candidate, jobs, sources),
        # Whether anything here still moves on its own. Read from the job states
        # for the same reason the queue does: the candidate's own status lags a
        # task by one transition.
        "live": is_live(candidate.status)
        or any(is_live(job.state) for job in jobs),
    }


@router.get("/works/{candidate_id}")
async def get_work(request: Request, candidate_id: int) -> dict:
    """Detail, metadata, audit timeline and related tasks for one work."""
    deps.require_session(request)
    snapshot = await work_snapshot(
        deps.database(request),
        candidate_id,
        download=deps.optional_service(request, "download_service"),
        sources=configured_sources(request),
    )
    if snapshot is None:
        raise ApiError(
            "CANDIDATE_NOT_FOUND",
            "候选不存在或已被删除",
            status_code=404,
        )
    return snapshot


def configured_sources(request: Request) -> frozenset[str]:
    """Which providers this deployment can actually reach.

    A source whose service was never configured is absent rather than offered:
    the enqueue would fail at the first call, and an operator has no way to tell
    that button from a working one until it does.
    """
    found = {PROVIDER_TELEGRAM}
    manager = deps.optional_service(request, "connection_manager")
    if manager is not None and manager.user_download_available():
        found.add(PROVIDER_TELEGRAM_USER)
    if deps.optional_service(request, "torrent_service") is not None:
        found.add(PROVIDER_EH_TORRENT)
    if deps.optional_service(request, "exhentai_service") is not None:
        found.add(PROVIDER_EXHENTAI)
    if deps.optional_service(request, "telegraph_service") is not None:
        found.add(PROVIDER_TELEGRAPH)
    return frozenset(found)


__all__ = [
    "configured_sources",
    "router",
    "work_actions",
    "work_snapshot",
    "work_stage",
    "work_timeline",
]
