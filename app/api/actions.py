"""State-changing endpoints.

Every route here goes through `require_session` then `require_csrf`, in that
order, so an expired session reads as 401 rather than as a confusing CSRF
failure. Domain errors carry a stable ``code`` and an operator-facing
``public_message`` already, so they are translated into the JSON envelope
verbatim instead of being reworded here.

Review orchestration is delegated to `ReviewOrchestrator`, the same object the
HTML routes use. That shared path is what guarantees the API cannot approve a
candidate the page would have refused.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError
from app.api.events import EVENT_CANDIDATE, EVENT_DOWNLOAD
from app.candidates import manual as manual_ingest
from app.conversion.convert import ConversionError
from app.db.database import DuplicateGalleryCandidate
from app.downloads.models import (
    MAX_JOB_PRIORITY,
    MIN_JOB_PRIORITY,
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
    PROVIDER_TELEGRAM_USER,
    PROVIDER_TELEGRAPH,
)
from app.downloads.service import DownloadError
from app.exhentai.service import ExHentaiDownloadError
from app.review.models import METADATA_FIELDS, editable_metadata_field
from app.conversion.comicinfo import EDITABLE_ELEMENTS
from app.review.service import ReviewError, ReviewService
from app.review.orchestration import select_telegram_attachment


router = APIRouter(tags=["actions"])

#: Batch ceiling. A larger request is refused rather than truncated: silently
#: acting on the first N of a selection the operator made is worse than telling
#: them to narrow it.
MAX_BATCH = 100

#: Job actions mapped to the `DownloadService` method that performs them. A
#: table keeps the URL surface and the service in step, and means an unknown
#: action is a 400 from one check rather than a chain of `elif`s.
JOB_ACTIONS: dict[str, str] = {
    "retry": "retry_job",
    "pause": "pause_job",
    "resume": "resume_job",
    "cancel": "cancel_job",
    "stop-seeding": "stop_seeding",
}

#: The two actions that take an argument, so they cannot live in the table
#: above. They are still valid in a batch: promoting five jobs at once, or
#: moving a stalled selection to another source, is the reason batch exists.
JOB_ACTION_SWITCH_SOURCE = "switch-source"
JOB_ACTION_PRIORITY = "priority"

BATCH_JOB_ACTIONS: frozenset[str] = frozenset(JOB_ACTIONS) | {
    JOB_ACTION_SWITCH_SOURCE,
    JOB_ACTION_PRIORITY,
}

#: The sources a work's archive can be pulled from, mapped to the
#: `DownloadService` method each one is queued through. The keys are the same
#: providers `app/api/works.py::work_actions` offers in `actions.sources[]`, so
#: the detail page's buttons and this endpoint cannot disagree about which
#: sources exist; `PROVIDER_CONVERSION` is absent because packaging is not a
#: source an operator picks.
SOURCE_ENQUEUE: dict[str, str] = {
    PROVIDER_TELEGRAM: "enqueue_telegram_download",
    PROVIDER_TELEGRAM_USER: "enqueue_telegram_user_download",
    PROVIDER_EXHENTAI: "enqueue_exhentai_download",
    PROVIDER_TELEGRAPH: "enqueue_telegraph_download",
    PROVIDER_EH_TORRENT: "enqueue_torrent_download",
}


async def _guard(request: Request) -> str:
    """Authenticate, verify CSRF, and return the operator name."""
    await deps.require_session(request)
    deps.require_csrf(request)
    return str(request.session.get("username") or "admin")


def _event_bus(request: Request):
    return getattr(request.app.state, "event_bus", None)


def _publish(request: Request, name: str, **data: Any) -> None:
    """Announce a transition, if anyone is listening.

    Never allowed to break the operation it reports: the write has already
    been committed by this point, so a broken stream must not turn a successful
    approval into a 500.
    """
    bus = _event_bus(request)
    if bus is None:
        return
    bus.publish(name, **data)


def _int_ids(
    raw: Any,
    *,
    required_code: str,
    required_message: str,
    invalid_code: str,
    invalid_message: str,
    too_large_message: str,
) -> list[int]:
    """Validate, de-duplicate and bound a list of row ids.

    Shared by the two batch endpoints so\u300c\u7a7a\u9009\u62e9\u300d\u3001\u300c\u4e0d\u662f\u6570\u5b57\u300dand\u300c\u4e00\u6b21\u592a\u591a\u300d
    behave identically whether the operator selected candidates or jobs; only
    the error codes differ, because a client tells them apart by code.
    """
    if not isinstance(raw, list) or not raw:
        raise ApiError(required_code, required_message)
    try:
        ids = list(dict.fromkeys(int(value) for value in raw))
    except (TypeError, ValueError) as exc:
        raise ApiError(invalid_code, invalid_message) from exc
    if len(ids) > MAX_BATCH:
        raise ApiError(
            "BATCH_TOO_LARGE",
            too_large_message,
            details={"max": MAX_BATCH, "received": len(ids)},
        )
    return ids


def _candidate_ids(raw: Any) -> list[int]:
    """Validate and de-duplicate the requested candidate ids."""
    return _int_ids(
        raw,
        required_code="CANDIDATE_IDS_REQUIRED",
        required_message="\u8bf7\u81f3\u5c11\u9009\u62e9\u4e00\u4e2a\u5019\u9009",
        invalid_code="CANDIDATE_IDS_INVALID",
        invalid_message="\u5019\u9009\u7f16\u53f7\u65e0\u6548",
        too_large_message=(
            f"\u5355\u6b21\u6700\u591a\u5904\u7406 {MAX_BATCH} \u4e2a\u5019\u9009"
        ),
    )


def _job_ids(raw: Any) -> list[int]:
    """Validate and de-duplicate the requested job ids."""
    return _int_ids(
        raw,
        required_code="JOB_IDS_REQUIRED",
        required_message="\u8bf7\u81f3\u5c11\u9009\u62e9\u4e00\u4e2a\u4efb\u52a1",
        invalid_code="JOB_IDS_INVALID",
        invalid_message="\u4efb\u52a1\u7f16\u53f7\u65e0\u6548",
        too_large_message=f"\u5355\u6b21\u6700\u591a\u5904\u7406 {MAX_BATCH} \u4e2a\u4efb\u52a1",
    )


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ApiError("BODY_INVALID", "\u8bf7\u6c42\u4f53\u4e0d\u662f\u5408\u6cd5 JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "\u8bf7\u6c42\u4f53\u5fc5\u987b\u662f\u5bf9\u8c61")
    return payload


def _required_provider(payload: dict) -> str:
    return _check_provider(payload.get("provider"))


def _check_provider(raw: object) -> str:
    provider = str(raw or "")
    if not provider:
        raise ApiError("PROVIDER_REQUIRED", "\u8bf7\u6307\u5b9a\u76ee\u6807\u4e0b\u8f7d\u6765\u6e90")
    return provider


def _required_priority(payload: dict) -> int:
    return _check_priority(payload.get("priority"))


def _check_priority(raw: object) -> int:
    """Read and bound the requested queue position.

    The range is checked here as well as in the service: a batch validates its
    argument once, before touching any job, and an out-of-range value should
    read as\u300c\u4f60\u53d1\u7684\u53c2\u6570\u4e0d\u5bf9\u300drather than as the first job of fifty failing.
    """
    if raw is None:
        raise ApiError("PRIORITY_REQUIRED", "\u8bf7\u6307\u5b9a\u4f18\u5148\u7ea7")
    try:
        priority = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError("PRIORITY_INVALID", "\u4f18\u5148\u7ea7\u5fc5\u987b\u662f\u6574\u6570") from exc
    if priority < MIN_JOB_PRIORITY or priority > MAX_JOB_PRIORITY:
        raise ApiError(
            "PRIORITY_OUT_OF_RANGE",
            f"\u4f18\u5148\u7ea7\u9700\u5728 {MIN_JOB_PRIORITY} \u5230 {MAX_JOB_PRIORITY} \u4e4b\u95f4"
            "\uff0c\u6570\u503c\u8d8a\u5c0f\u8d8a\u9760\u524d",
            details={"min": MIN_JOB_PRIORITY, "max": MAX_JOB_PRIORITY},
        )
    return priority


#: Review actions a batch may run. Named here so the JSON endpoint and the
#: `/candidates` form fallback reject the same set of words.
REVIEW_BATCH_ACTIONS: frozenset[str] = frozenset({"approve", "reject"})


async def apply_review_batch(
    orchestrator,
    action: str,
    candidate_ids: list[int],
    operator: str,
    *,
    announce_candidate: Callable[[int], None] | None = None,
    announce_job: Callable[[int], None] | None = None,
) -> dict:
    """Approve or reject a selection, one candidate at a time.

    Shared by the JSON endpoint below and by the HTML form fallback on
    `/candidates`, so the two can never disagree about what a batch does; the
    action name is checked here rather than in each caller, because the form
    path posts a raw field and would otherwise reach the orchestrator with
    whatever the operator's browser sent.

    `approve_and_enqueue` is deliberately all-or-nothing over the ids it gets,
    so this passes it **one id at a time**: each candidate is still validated and
    routed atomically, but a selection containing one already-approved or
    unroutable item no longer refuses the other forty-nine. That is what makes a
    batch replay-safe -- re-sending it after a partial run approves only what is
    still pending and reports the rest under ``skipped`` with the reason, which
    is what an operator who double-clicked\u300c\u5168\u90e8\u901a\u8fc7\u300dneeds. Auditing is
    unaffected: the orchestrator writes one `review_actions` row per candidate it
    actually acts on, so a skip leaves no trace of an action that did not happen.
    """
    if action not in REVIEW_BATCH_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"\u672a\u77e5\u7684\u5ba1\u6838\u52a8\u4f5c\uff1a{action}",
            details={"allowed": sorted(REVIEW_BATCH_ACTIONS)},
        )

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        try:
            if action == "approve":
                job_ids = await orchestrator.approve_and_enqueue(
                    [candidate_id], operator
                )
            else:
                await orchestrator.reject([candidate_id], operator)
                job_ids = ()
        except Exception as exc:  # noqa: BLE001 - re-raised when unexpected
            translated = _translate(exc)
            if not isinstance(translated, ApiError):
                # A domain refusal is a skip; anything else is a fault, and
                # reporting it as a skip would hide it behind a tidy 200.
                raise translated from exc
            skipped.append(
                {
                    "candidate_id": candidate_id,
                    "code": translated.code,
                    "message": translated.message,
                }
            )
            continue
        applied.append(
            {"candidate_id": candidate_id, "job_ids": [int(j) for j in job_ids]}
        )
        if announce_candidate is not None:
            announce_candidate(candidate_id)
        if announce_job is not None:
            for job_id in job_ids:
                announce_job(int(job_id))

    return {
        "action": action,
        "requested": len(candidate_ids),
        "applied": applied,
        "skipped": skipped,
    }


#: The two 彻底删除 actions a batch may run. Split exactly like `remove` and
#: `remove-files` on `/downloaded`: the destructive one is a separate word so a
#: mis-sent action cannot be the one that also wipes the files, and so the two
#: dialogs can say which one the operator pressed.
CANDIDATE_DELETE_ACTIONS: frozenset[str] = frozenset({"delete", "delete-files"})


async def apply_candidate_delete_batch(
    archived_service,
    action: str,
    candidate_ids: list[int],
    operator: str,
    *,
    announce_candidate: Callable[[int], None] | None = None,
) -> dict:
    """Purge a selection of candidates, records and optionally files.

    One candidate at a time, for the reason `apply_review_batch` acts per
    candidate: a missing work must not refuse the
    other forty-nine. A refused work -- missing row -- is reported
    under ``skipped`` with the reason, and re-sending the batch finishes what a
    half-failed first attempt started.

    A genuine fault is re-raised rather than folded into ``skipped``: a broken
    filesystem must not read as 「49 已删除，1 跳过」.
    """
    if action not in CANDIDATE_DELETE_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的删除动作：{action}",
            details={"allowed": sorted(CANDIDATE_DELETE_ACTIONS)},
        )
    delete_files = action == "delete-files"

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        try:
            result = await archived_service.purge_work(
                candidate_id,
                delete_files=delete_files,
                operator_name=operator,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised when unexpected
            translated = _translate(exc)
            if not isinstance(translated, ApiError):
                raise translated from exc
            skipped.append(
                {
                    "candidate_id": candidate_id,
                    "code": translated.code,
                    "message": translated.message,
                }
            )
            continue
        applied.append(
            {
                "candidate_id": candidate_id,
                "removed_jobs": result["removed_jobs"],
                "removal_id": result["removal_id"],
                "operation_ids": result["operation_ids"],
                "cleanup_status": result["cleanup_status"],
                "deleted_files": list(result["deleted_files"]),
                "failed_files": list(result["failed_files"]),
            }
        )
        if announce_candidate is not None:
            announce_candidate(candidate_id)

    return {
        "action": action,
        "requested": len(candidate_ids),
        "applied": applied,
        "skipped": skipped,
    }


@router.post("/candidates/batch")
async def api_batch_review(request: Request) -> dict:
    """Run one batch action against a selection of candidates.

    Approve / reject go through `apply_review_batch`; the two 彻底删除 actions go
    through `apply_candidate_delete_batch`. Both are the coroutines the
    `/candidates` form posts through, so the API and the page cannot disagree
    about what a batch does or about which rows it skips.

    Reports per-candidate outcomes instead of failing the selection on the first
    refusal. The dispatcher duplicates the page's, on purpose: the two sets are
    named here and there, and the shared coroutines are what keep the behaviour
    identical.
    """
    operator = await _guard(request)
    payload = await _body(request)
    action = str(payload.get("action") or "")
    candidate_ids = _candidate_ids(payload.get("candidate_ids"))
    if action in CANDIDATE_DELETE_ACTIONS:
        return await apply_candidate_delete_batch(
            deps.archived_work_service(request),
            action,
            candidate_ids,
            operator,
            announce_candidate=lambda candidate_id: _publish(
                request, EVENT_CANDIDATE, candidate_id=candidate_id
            ),
        )
    if action not in REVIEW_BATCH_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的审核动作：{action}",
            details={
                "allowed": sorted(
                    REVIEW_BATCH_ACTIONS | CANDIDATE_DELETE_ACTIONS
                )
            },
        )
    return await apply_review_batch(
        deps.review_orchestrator(request),
        action,
        candidate_ids,
        operator,
        announce_candidate=lambda candidate_id: _publish(
            request, EVENT_CANDIDATE, candidate_id=candidate_id
        ),
        announce_job=lambda job_id: _publish(
            request, EVENT_DOWNLOAD, job_id=job_id
        ),
    )


@router.post("/candidates/manual")
async def manual_add(request: Request) -> dict:
    """Turn a pasted ExHentai gallery link into an approved, queued candidate.

    The sequence -- shape, then duplicate, then the gallery -- is
    `app.candidates.manual`'s, the same one the `/manual-add` page runs. The
    connection gate is checked before the link is even looked at, for the page's
    reason: a deployment with no Cookie cannot fetch the gallery that validates
    the link, so a deployment is told what to fix rather than that its link is
    unreadable.
    """
    await _guard(request)
    payload = await _body(request)
    manager = deps.optional_service(request, "connection_manager")
    if not manual_ingest.exhentai_configured(manager):
        raise ApiError(
            "EXHENTAI_NOT_CONFIGURED", manual_ingest.NOT_CONFIGURED_MESSAGE
        )
    try:
        candidate_id = await manual_ingest.ingest_manual_link(
            str(payload.get("url") or ""),
            database=deps.database(request),
            exhentai_service=deps.exhentai_service(request),
            orchestrator=deps.review_orchestrator(request),
            download_service=deps.download_service(request),
        )
    except DuplicateGalleryCandidate as exc:
        # Not an error to fix: the work is already here, and the client's move is
        # to open it. Carried as a code plus the id so it can do that without
        # parsing the message.
        raise ApiError(
            "CANDIDATE_DUPLICATE",
            f"作品已存在：候选 #{exc.existing_candidate_id}",
            status_code=409,
            details={"existing_candidate_id": exc.existing_candidate_id},
        ) from exc
    except (ReviewError, ExHentaiDownloadError) as exc:
        raise ApiError(
            str(getattr(exc, "code", "MANUAL_ADD_FAILED")),
            str(getattr(exc, "public_message", exc)),
        ) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return {"candidate_id": candidate_id}


async def apply_job_batch(
    service,
    action: str,
    job_ids: list[int],
    *,
    provider: str | None = None,
    priority: int | None = None,
    announce: Callable[[int], None] | None = None,
    preconditions: dict | None = None,
) -> dict:
    """Run one lifecycle action against a selection of tasks.

    Shared by the JSON endpoint below and by the HTML form fallback on
    `/activity`, so the two can never disagree about what a batch does. Each
    caller reads its argument out of its own request shape (a JSON body, a form
    field) and hands the raw value here; the *checking* is done here, once, so a
    caller cannot forget it. The form path did, and a `priority` batch posted
    with no number reached `set_job_priority(job_id, None)` and came back a 500.
    Both checks run before any job is touched.

    Each selected task carries its observed identity, revision and durable
    idempotency key. Replays return each original receipt; a changed or removed
    task is reported under skipped without preventing the remaining items.
    """
    params = {}
    if action == JOB_ACTION_SWITCH_SOURCE:
        params["provider"] = _check_provider(provider)
    elif action == JOB_ACTION_PRIORITY:
        params["priority"] = _check_priority(priority)
    if not isinstance(preconditions, dict):
        raise ApiError("PRECONDITIONS_REQUIRED", "请为每项任务提供已读取的身份、版本与幂等键")
    if any(not isinstance(preconditions.get(str(job_id)), dict) for job_id in job_ids):
        raise ApiError("PRECONDITIONS_REQUIRED", "请为每项任务提供已读取的身份、版本与幂等键")

    async def run(job_id: int):
        observed = preconditions.get(str(job_id), {})
        return await service.command(job_id, action, **command_preconditions(observed), **params)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for job_id in job_ids:
        try:
            result = await run(job_id)
        except Exception as exc:  # noqa: BLE001 - re-raised when unexpected
            translated = _translate(exc)
            if not isinstance(translated, ApiError):
                # Not a domain refusal but a genuine fault. Surfacing it as a
                # skip would hide a broken queue behind a tidy 200.
                raise translated from exc
            skipped.append(
                {
                    "job_id": job_id,
                    "code": translated.code,
                    "message": translated.message,
                }
            )
            continue
        applied.append({"job_id": job_id, "result": result})
        if announce is not None:
            # STOPPING announces the accepted operation, not a new transfer.
            announce(result["job_id"])

    return {
        "action": action,
        "requested": len(job_ids),
        "applied": applied,
        "skipped": skipped,
    }


def command_preconditions(payload: dict) -> dict:
    """Do not fill client drafts with the server's latest task revision."""
    return {"task_id": payload.get("task_id"), "expected_revision": payload.get("expected_revision"),
            "idempotency_key": payload.get("idempotency_key")}


@router.post("/jobs/batch")
async def batch_job_action(request: Request) -> dict:
    await _guard(request)
    payload = await _body(request)
    action = str(payload.get("action") or "")
    if action not in BATCH_JOB_ACTIONS:
        raise ApiError("ACTION_UNKNOWN", "未知的任务动作", details={"allowed": sorted(BATCH_JOB_ACTIONS)})
    return await apply_job_batch(deps.download_service(request), action, _job_ids(payload.get("job_ids")),
        provider=payload.get("provider"), priority=payload.get("priority"), preconditions=payload.get("preconditions"),
        announce=lambda job_id: _publish(request, EVENT_DOWNLOAD, job_id=job_id))


async def apply_job_command(request: Request, job_id: int, action: str, payload: dict) -> dict:
    params = {}
    if action == "priority":
        params["priority"] = _required_priority(payload)
    elif action == "switch-source":
        params["provider"] = _required_provider(payload)
    elif action not in JOB_ACTIONS:
        raise ApiError("ACTION_UNKNOWN", "未知的任务动作", details={"allowed": sorted(JOB_ACTIONS)})
    try:
        result = await deps.download_service(request).command(job_id, action, **command_preconditions(payload), **params)
    except Exception as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=result["job_id"])
    return result


@router.post("/jobs/{job_id}/priority")
async def set_priority(request: Request, job_id: int) -> dict:
    await _guard(request)
    return await apply_job_command(request, job_id, "priority", await _body(request))


@router.post("/jobs/{job_id}/switch-source")
async def switch_source(request: Request, job_id: int) -> dict:
    await _guard(request)
    return await apply_job_command(request, job_id, "switch-source", await _body(request))


@router.post("/jobs/{job_id}/{action}")
async def job_action(request: Request, job_id: int, action: str) -> dict:
    await _guard(request)
    return await apply_job_command(request, job_id, action, await _body(request))


@router.patch("/works/{candidate_id}/metadata")
async def patch_metadata(request: Request, candidate_id: int) -> dict:
    """Apply operator overrides and field locks to one or more metadata fields.

    Writes go through `ReviewService`, which re-evaluates the source rules after
    each change: editing a field can move a candidate out of NEEDS_INFO, and
    that has to happen here rather than on the next ingest.

    `fields` and `locks` are independent, and either alone is a valid request:
    an operator can retype a value, pin one ExHentai already supplied, or do
    both. Locks are applied after the edits so `{"fields": {"Title": "x"},
    "locks": {"Title": true}}` pins the value it just wrote rather than the one
    it replaced.
    """
    operator = await _guard(request)
    payload = await _body(request)
    return await apply_metadata_patch(request, candidate_id, payload, operator)


async def apply_metadata_patch(request: Request, candidate_id: int, payload: dict, operator: str) -> dict:
    """Validated edit shared by JSON and the progressive HTML editor."""
    fields = payload.get("fields")
    locks = payload.get("locks")
    fetched = payload.get("fetched_fields", {})
    if not isinstance(fetched, dict) or any(name not in EDITABLE_ELEMENTS or not isinstance(value, str)
                                           for name, value in fetched.items()):
        raise ApiError("FETCHED_FIELDS_INVALID", "fetched_fields 必须是可编辑 ComicInfo 字段到字符串的映射")
    if fields is not None and not isinstance(fields, dict):
        raise ApiError("FIELDS_INVALID", "fields \u5fc5\u987b\u662f\u5b57\u6bb5\u5230\u503c\u7684\u6620\u5c04")
    if locks is not None and not isinstance(locks, dict):
        raise ApiError("LOCKS_INVALID", "locks \u5fc5\u987b\u662f\u5b57\u6bb5\u5230\u5e03\u5c14\u503c\u7684\u6620\u5c04")
    fields = fields or {}
    locks = locks or {}
    if not fields and not locks and not fetched:
        raise ApiError(
            "FIELDS_REQUIRED", "\u8bf7\u63d0\u4f9b\u8981\u4fee\u6539\u7684\u5b57\u6bb5"
        )
    unknown = sorted(name for name in set(fields) | set(locks)
                     if not editable_metadata_field(name))
    if unknown:
        raise ApiError(
            "METADATA_FIELD_INVALID",
            f"\u4e0d\u652f\u6301\u7684\u5b57\u6bb5\uff1a{', '.join(unknown)}",
            details={"unknown": unknown, "allowed": list(METADATA_FIELDS)},
        )

    database = deps.database(request)
    if await database.get_candidate(candidate_id) is None:
        raise ApiError(
            "CANDIDATE_NOT_FOUND",
            "\u5019\u9009\u4e0d\u5b58\u5728\u6216\u5df2\u88ab\u5220\u9664",
            status_code=404,
        )

    service = ReviewService(database)
    try:
        for field_name, value in fields.items():
            await service.set_manual_metadata(
                candidate_id, operator, field_name, str(value)
            )
        for field_name, locked in locks.items():
            await service.set_metadata_lock(
                candidate_id, operator, field_name, bool(locked)
            )
    except ReviewError as exc:
        raise ApiError(exc.code, exc.public_message) from exc

    archived = deps.archived_work_service(request)
    sync = await archived.sync_archive_metadata(candidate_id, fields=fields, fetched_fields=fetched, operator_name=operator)

    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return {
        "candidate_id": candidate_id,
        "updated": sorted(fields),
        "locked": sorted(name for name, on in locks.items() if on),
        "unlocked": sorted(name for name, on in locks.items() if not on),
        "archive_synced": sync["synced"],
        "sync_skipped": sync.get("reason"),
        "sync_message": sync["message"],
        "metadata": await database.effective_metadata(candidate_id),
        "metadata_editor": await archived.metadata_editor(candidate_id),
    }


@router.post("/works/{candidate_id}/requeue")
async def requeue_work(request: Request, candidate_id: int) -> dict:
    """Put a work the operator or a rule set aside back into the review queue.

    Runs through `ReviewService.requeue_candidate`, the coroutine the work detail
    page posts to, so the JSON layer cannot requeue a work the page would have
    refused: whether a status may be requeued is decided by the service
    (`REQUEUEABLE_STATUSES`), once, for both layers.

    The body is optional. `note` is the operator's own sentence for the audit
    row; omitting it is the common case, not an error.
    """
    operator = await _guard(request)
    payload = await _body(request)
    note = str(payload.get("note") or "").strip() or None
    service = ReviewService(deps.database(request))
    try:
        await service.requeue_candidate(candidate_id, operator, note)
    except ReviewError as exc:
        # Same envelope as the metadata route beside it: the service owns the
        # refusal and its wording, and a missing row is reported by it rather
        # than re-derived here.
        raise ApiError(exc.code, exc.public_message) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return {"candidate_id": candidate_id, "status": "PENDING_REVIEW"}


@router.post("/works/{candidate_id}/convert")
async def convert_work(request: Request, candidate_id: int) -> dict:
    """Queue packaging for a work; pressing it again is a re-pack.

    One endpoint for both, because `ConversionService.enqueue_for_candidate` is
    idempotent per work: the page's 打包为 CBZ and 重新打包 are the same button
    with two labels, and a second endpoint would be a second place that decides
    what "already packed" means.

    The response carries the queued job id: the client needs it to follow the
    task it just started, and the page only ever re-read it off the work.
    """
    await _guard(request)
    if await deps.database(request).get_candidate(candidate_id) is None:
        raise ApiError(
            "CANDIDATE_NOT_FOUND", "候选不存在或已被删除", status_code=404
        )
    service = deps.conversion_service(request)
    try:
        job_id = await service.enqueue_for_candidate(candidate_id)
    except ConversionError as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=job_id)
    return {"candidate_id": candidate_id, "job_id": job_id}


@router.post("/works/{candidate_id}/fetch-metadata")
async def fetch_work_metadata(request: Request, candidate_id: int) -> dict:
    """Re-read the gallery's metadata for one work.

    Runs `ExHentaiService.fetch_metadata_for_candidate`, the call the work detail
    page makes: read the gallery, then persist it through the one shared writer
    (`persist_gallery_metadata`) so the JSON path cannot store a different subset
    of the same gallery than the page does.

    The refusals are the service's, and they are the ones an operator actually
    hits: no ExHentai connection configured, no gallery on this candidate, or an
    upstream that answered with something other than a gallery.
    """
    await _guard(request)
    database = deps.database(request)
    candidate = await database.get_candidate(candidate_id)
    if candidate is None:
        raise ApiError(
            "CANDIDATE_NOT_FOUND", "候选不存在或已被删除", status_code=404
        )
    try:
        fetched = await deps.exhentai_service(request).fetch_metadata_for_candidate(
            candidate_id
        )
    except ExHentaiDownloadError as exc:
        raise ApiError(exc.code, exc.public_message) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return {
        "candidate_id": candidate_id,
        "has_gallery": candidate.ex_gid is not None,
        "metadata_editor": await deps.archived_work_service(request).metadata_editor(candidate_id, fetched_names=fetched),
    }


@router.post("/works/{candidate_id}/sources/{provider}")
async def pull_work_source(
    request: Request, candidate_id: int, provider: str
) -> dict:
    """Pull this work's archive from one named source, as the page's buttons do.

    Every call is `redownload=True`, for the page's reason: pressing a source
    button on a work that already came from it means 「再拉一次」 -- that is what
    makes an old failure recoverable, where a no-op press would leave the bad
    file on disk for every later pack to read.

    The two Telegram routes need an archive attachment to name; a candidate
    without one is refused with the page's sentence rather than with an
    `AttributeError` from inside the download service.

    `EXHENTAI` is ExHentai's Archive Download, which spends GP. The
    confirmation is the client's business -- the page asks before it posts, and
    this endpoint does not second-guess an operator who pressed it.
    """
    await _guard(request)
    target = provider.strip().upper()
    if target not in SOURCE_ENQUEUE:
        raise ApiError(
            "PROVIDER_UNKNOWN",
            f"未知的来源：{provider}",
            details={"allowed": sorted(SOURCE_ENQUEUE)},
        )
    database = deps.database(request)
    candidate = await database.get_candidate(candidate_id)
    if candidate is None:
        raise ApiError(
            "CANDIDATE_NOT_FOUND", "候选不存在或已被删除", status_code=404
        )
    service = deps.download_service(request)
    try:
        if target == PROVIDER_TELEGRAM:
            result = await service.enqueue_telegram_download(
                candidate_id, _archive_attachment(candidate, target), redownload=True
            )
        elif target == PROVIDER_TELEGRAM_USER:
            result = await service.enqueue_telegram_user_download(
                candidate_id, _archive_attachment(candidate, target), redownload=True
            )
        elif target == PROVIDER_TELEGRAPH:
            # Presence check before the enqueue, exactly as the page does it: a
            # deployment with no Telegraph client should be told that, not given
            # a job that can only fail.
            deps.telegraph_service(request)
            result = await service.enqueue_telegraph_download(
                candidate_id, redownload=True
            )
        elif target == PROVIDER_EH_TORRENT:
            deps.torrent_service(request)
            result = await service.enqueue_torrent_download(
                candidate_id, redownload=True
            )
        else:  # PROVIDER_EXHENTAI
            result = await service.enqueue_exhentai_download(
                candidate_id, redownload=True
            )
    except DownloadError as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=result.job_id)
    return {
        "candidate_id": candidate_id,
        "provider": target,
        "job_id": result.job_id,
        "task_id": result.task_id,
        "revision": result.revision,
    }


def _archive_attachment(candidate: Any, provider: str = PROVIDER_TELEGRAM) -> dict:
    """The first archive attachment on a candidate, or the page's refusal.

    Same guard and same sentence as `/candidates/{id}/download`: a Bot API pull
    has to name the attachment it is fetching, and a candidate that carries none
    is the operator on the wrong work rather than a broken server.
    """
    archives = [attachment for message in candidate.messages for attachment in message.attachments
                if attachment.get("type") == "archive"]
    selected = select_telegram_attachment(archives, provider)
    if selected is not None:
        return dict(selected)
    raise ApiError("ARCHIVE_ATTACHMENT_MISSING", "该候选没有可下载的压缩附件")


def _translate(exc: Exception) -> Exception:
    """Map a domain error onto the JSON envelope.

    Anything without a ``code``/``public_message`` pair is a genuine bug and is
    returned unchanged so it surfaces as a 500 instead of being disguised as a
    tidy 400.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "public_message", None)
    if code and message:
        # A missing row is a 404 rather than a 400: the request was well formed,
        # and the interface needs to tell「你发的参数不对」from「这条已经没了」
        # in order to decide between showing an error and refreshing the list.
        status = 404 if str(code).endswith("_NOT_FOUND") else 400
        if code in {"REVISION_CONFLICT", "TASK_SUPERSEDED", "IDEMPOTENCY_CONFLICT", "WORK_SLOT_OCCUPIED",
                    "OPERATION_IN_PROGRESS", "REMOTE_RECONCILIATION_REQUIRED", "SOURCE_CHANGED"}:
            status = 409
        return ApiError(str(code), str(message), status_code=status)
    return exc


__all__ = [
    "BATCH_JOB_ACTIONS",
    "CANDIDATE_DELETE_ACTIONS",
    "JOB_ACTIONS",
    "JOB_ACTION_PRIORITY",
    "JOB_ACTION_SWITCH_SOURCE",
    "MAX_BATCH",
    "REVIEW_BATCH_ACTIONS",
    "apply_candidate_delete_batch",
    "apply_job_batch",
    "apply_job_command",
    "apply_review_batch",
    "router",
]
