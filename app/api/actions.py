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
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.contracts import ApiError
from app.api.events import EVENT_CANDIDATE, EVENT_DOWNLOAD
from app.candidates import manual as manual_ingest
from app.conversion.convert import ConversionError
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
from app.downloads.archived import ArchivedWorkError
from app.exhentai.service import ExHentaiDownloadError
from app.review.models import METADATA_FIELDS, editable_metadata_field
from app.conversion.comicinfo import EDITABLE_ELEMENTS
from app.review.service import ReviewError
from app.review.application import edit_metadata, fetch_metadata, enqueue_work, remove_work
from app.review.batches import ReviewBatches
from app.review.commands import WorkCommandError, WorkCommands
from app.review.manual import add_manual_work
from app.api.works import configured_sources


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
    return await deps.operator_name(request)


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
    return deps.command_payload(request, payload)


async def _source_correction(request, candidate_id, action):
    from app.review.source_commands import SourceCommands
    await _guard(request)
    try:
        result = await SourceCommands(deps.database(request)).execute(candidate_id, action, await _body(request))
    except ReviewError as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return JSONResponse(result, status_code=202 if result['status'] == 'PENDING' else 200,
                        headers={'Cache-Control': 'no-store'})


@router.put('/works/{candidate_id}/eh-ref')
async def api_set_work_eh_ref(request: Request, candidate_id: int):
    return await _source_correction(request, candidate_id, 'eh-ref')


@router.put('/works/{candidate_id}/preview')
async def api_set_work_preview(request: Request, candidate_id: int):
    return await _source_correction(request, candidate_id, 'preview')


@router.put('/works/{candidate_id}/torrent')
async def api_set_work_torrent(request: Request, candidate_id: int):
    return await _source_correction(request, candidate_id, 'torrent')


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
    versions: dict | None = None,
    idempotency_key: str | None = None,
    announce_candidate: Callable[[int], None] | None = None,
    announce_job: Callable[[int], None] | None = None,
) -> dict:
    """Shared HTTP/form adapter for durable per-work review outcomes."""
    try:
        result = await orchestrator.review_batch(action, candidate_ids, versions, idempotency_key, operator)
    except (ReviewError, DownloadError) as exc:
        raise _translate(exc) from exc
    for item in result['applied']:
        if announce_candidate is not None:
            announce_candidate(item['candidate_id'])
        if announce_job is not None:
            for job_id in item['job_ids']:
                announce_job(job_id)
    return result


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
    versions: dict | None = None,
    idempotency_key: str | None = None,
    announce_candidate: Callable[[int], None] | None = None,
) -> dict:
    """Accept durable purges with observed versions and fixed child outcomes."""
    if action not in CANDIDATE_DELETE_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的删除动作：{action}",
            details={"allowed": sorted(CANDIDATE_DELETE_ACTIONS)},
        )
    try:
        result = await ReviewBatches(archived_service._database, archived=archived_service).run(
            action, candidate_ids, versions, idempotency_key, operator)
    except (ReviewError, ArchivedWorkError) as exc:
        raise _translate(exc) from exc
    for item in result['applied']:
        if announce_candidate is not None:
            announce_candidate(item['candidate_id'])
    return result


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
            versions=payload.get('versions'),
            idempotency_key=payload.get('idempotency_key'),
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
        versions=payload.get('versions'),
        idempotency_key=payload.get('idempotency_key'),
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
    try:
        result = await add_manual_work(deps.database(request), deps.exhentai_service(request),
            deps.review_orchestrator(request), deps.download_service(request), payload,
            configured=manual_ingest.exhentai_configured(manager))
    except WorkCommandError as exc:
        raise _translate(exc) from exc
    if result['status'] == 'COMPLETED':
        _publish(request, EVENT_CANDIDATE, candidate_id=result['candidate_id'])
    return JSONResponse(result, status_code=202 if result['status'] == 'RUNNING' else 200,
                        headers={'Cache-Control': 'no-store'})


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

    Writes go through the atomic work application, which re-evaluates source rules
    once after the whole edit: editing a field can move a candidate out of NEEDS_INFO, and
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
    try:
        result = await edit_metadata(database, deps.archived_work_service(request), candidate_id,
                                     {**payload, 'fields': fields, 'locks': locks}, operator)
    except WorkCommandError as exc:
        raise ApiError(exc.code, exc.public_message, status_code=exc.status_code, details=exc.details) from exc
    except ReviewError as exc:
        raise ApiError(exc.code, exc.public_message) from exc

    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return result


@router.post('/works/{candidate_id}/delete')
async def delete_work(request: Request, candidate_id: int):
    """Accept a purge once; read current stop/cleanup at /removals/{id}."""
    operator = await _guard(request)
    payload = await _body(request)
    mode = payload.get('mode', 'delete')
    if not isinstance(mode, str) or mode not in CANDIDATE_DELETE_ACTIONS:
        raise ApiError('ACTION_UNKNOWN', '不支持的删除动作')
    try:
        result = await remove_work(deps.database(request), deps.archived_work_service(request),
                                   candidate_id, mode, payload, operator)
    except (ReviewError, ArchivedWorkError) as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return JSONResponse(result, status_code=202)


@router.post("/works/{candidate_id}/review")
async def review_work(request: Request, candidate_id: int) -> dict:
    """Review the observed work; approval and its download intent commit together."""
    operator = await _guard(request)
    payload = await _body(request)
    try:
        result = await deps.review_orchestrator(request).work_command(candidate_id, payload.get('action'), payload, operator)
    except (ReviewError, DownloadError) as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    for job_id in result['job_ids']:
        _publish(request, EVENT_DOWNLOAD, job_id=job_id)
    return result


@router.post("/works/{candidate_id}/requeue")
async def requeue_work(request: Request, candidate_id: int) -> dict:
    """Legacy requeue URL mapped to the same versioned work command."""
    operator = await _guard(request)
    payload = await _body(request)
    try:
        result = await deps.review_orchestrator(request).work_command(candidate_id, 'requeue', payload, operator)
    except (ReviewError, DownloadError) as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return {**result, "status": "PENDING_REVIEW"}


@router.post("/works/{candidate_id}/convert")
async def convert_work(request: Request, candidate_id: int) -> dict:
    """Queue packaging for a work; pressing it again is a re-pack.

    The work application atomically commits the observed version, exact input,
    task and retry receipt. The page's 打包为 CBZ and 重新打包 use this same core.

    The response carries the queued job id: the client needs it to follow the
    task it just started, and the page only ever re-read it off the work.
    """
    await _guard(request)
    payload = await _body(request) if await request.body() else {}
    try:
        result = await enqueue_work(deps.database(request), deps.conversion_service(request), candidate_id,
                                    'CONVERSION', payload)
    except (WorkCommandError, ConversionError) as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=result['job_id'])
    return result


@router.post("/works/{candidate_id}/fetch-metadata")
async def fetch_work_metadata(request: Request, candidate_id: int) -> dict:
    """Re-read the gallery's metadata for one work.

    Shares the work-detail application service: accept a durable versioned
    operation, read the gallery outside the write transaction, then atomically
    commit metadata/torrents/cover and its result after rechecking the work.

    The refusals are the service's, and they are the ones an operator actually
    hits: no ExHentai connection configured, no gallery on this candidate, or an
    upstream that answered with something other than a gallery.
    """
    await _guard(request)
    database = deps.database(request)
    try:
        result = await fetch_metadata(database, deps.exhentai_service(request), deps.archived_work_service(request),
            candidate_id, await _body(request) if await request.body() else {})
    except WorkCommandError as exc:
        raise ApiError(exc.code, exc.public_message, status_code=exc.status_code, details=exc.details) from exc
    except ExHentaiDownloadError as exc:
        raise ApiError(exc.code, exc.public_message) from exc
    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return JSONResponse(result, status_code=202 if result['status'] == 'RUNNING' else 200)


@router.get("/metadata-fetches/{operation_id}")
async def metadata_fetch_operation(request: Request, operation_id: str) -> dict:
    """Read the saved result without dispatching another provider request."""
    await deps.require_session(request)
    try:
        return await WorkCommands(deps.database(request)).metadata_fetch_result(operation_id)
    except WorkCommandError as exc:
        raise ApiError(exc.code, exc.public_message, status_code=exc.status_code) from exc


@router.post("/works/{candidate_id}/sources/{provider}")
async def pull_work_source(
    request: Request, candidate_id: int, provider: str
) -> dict:
    """Pull this work's archive from one named source, as the page's buttons do.

    Each new command is `redownload=True`: pressing a source
    button on a work that already came from it means 「再拉一次」 -- that is what
    makes an old failure recoverable, where a no-op press would leave the bad
    file on disk for every later pack to read. Replaying the same command returns
    its original task before any routing or preparation and never revives it.

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
    payload = await _body(request) if await request.body() else {}
    try:
        result = await enqueue_work(deps.database(request), deps.download_service(request), candidate_id,
                                    target, payload, available=configured_sources(request))
    except (WorkCommandError, DownloadError) as exc:
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=result['job_id'])
    return result


def _translate(exc: Exception) -> Exception:
    """Map a domain error onto the JSON envelope.

    Anything without a ``code``/``public_message`` pair is a genuine bug and is
    returned unchanged so it surfaces as a 500 instead of being disguised as a
    tidy 400.
    """
    if isinstance(exc, WorkCommandError):
        return ApiError(exc.code, exc.public_message, status_code=exc.status_code, details=exc.details)
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
