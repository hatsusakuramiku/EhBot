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
from app.downloads.models import MAX_JOB_PRIORITY, MIN_JOB_PRIORITY
from app.review.models import METADATA_FIELDS
from app.review.service import ReviewError, ReviewService


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


def _guard(request: Request) -> str:
    """Authenticate, verify CSRF, and return the operator name."""
    deps.require_session(request)
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


@router.post("/candidates/batch")
async def batch_review(request: Request) -> dict:
    """Approve or reject several candidates at once."""
    operator = _guard(request)
    payload = await _body(request)
    action = str(payload.get("action") or "")
    if action not in {"approve", "reject"}:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"\u672a\u77e5\u7684\u5ba1\u6838\u52a8\u4f5c\uff1a{action}",
            details={"allowed": ["approve", "reject"]},
        )
    candidate_ids = _candidate_ids(payload.get("candidate_ids"))

    orchestrator = deps.review_orchestrator(request)
    try:
        if action == "approve":
            job_ids = await orchestrator.approve_and_enqueue(
                candidate_ids, operator
            )
        else:
            await orchestrator.reject(candidate_ids, operator)
            job_ids = ()
    except ReviewError as exc:
        raise ApiError(exc.code, exc.public_message) from exc

    for candidate_id in candidate_ids:
        _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    for job_id in job_ids:
        _publish(request, EVENT_DOWNLOAD, job_id=job_id)

    return {
        "action": action,
        "candidate_ids": candidate_ids,
        "job_ids": list(job_ids),
    }


async def apply_job_batch(
    service,
    action: str,
    job_ids: list[int],
    *,
    provider: str | None = None,
    priority: int | None = None,
    announce: Callable[[int], None] | None = None,
) -> dict:
    """Run one lifecycle action against a selection of tasks.

    Shared by the JSON endpoint below and by the HTML form fallback on
    `/activity`, so the two can never disagree about what a batch does. Each
    caller reads its argument out of its own request shape (a JSON body, a form
    field) and hands the raw value here; the *checking* is done here, once, so a
    caller cannot forget it. The form path did, and a `priority` batch posted
    with no number reached `set_job_priority(job_id, None)` and came back a 500.
    Both checks run before any job is touched.

    Idempotent by construction rather than by loosening the single-job writers:
    a job that cannot take the action -- already resumed, already cancelled,
    since deleted -- is reported under ``skipped`` with the reason, and the rest
    of the selection still runs. Re-sending the same batch is therefore safe and
    changes nothing the first one already did, which is what an operator
    double-clicking\u300c\u5168\u90e8\u6062\u590d\u300dneeds. Loosening `resume_job` itself would have
    cost the single-job path its error message, which is the one place an
    operator asks about exactly one job.
    """
    if action == JOB_ACTION_SWITCH_SOURCE:
        target = _check_provider(provider)

        async def run(job_id: int):
            return await service.switch_source(job_id, target)

    elif action == JOB_ACTION_PRIORITY:
        position = _check_priority(priority)

        async def run(job_id: int):
            return await service.set_job_priority(job_id, position)

    else:
        method_name = JOB_ACTIONS[action]

        async def run(job_id: int):
            return await getattr(service, method_name)(job_id)

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
            # Switching source replaces the job, so the id worth announcing is
            # the new one; every other action keeps working on the same row.
            announce(
                int(result) if action == JOB_ACTION_SWITCH_SOURCE else job_id
            )

    return {
        "action": action,
        "requested": len(job_ids),
        "applied": applied,
        "skipped": skipped,
    }


@router.post("/jobs/batch")
async def batch_job_action(request: Request) -> dict:
    """Run one lifecycle action against a selection of tasks.

    The argument is validated before any job is touched, so a bad provider name
    cannot leave half a selection switched and half not. The loop itself lives
    in `apply_job_batch`, which the `/activity` form posts through as well.
    """
    _guard(request)
    payload = await _body(request)
    action = str(payload.get("action") or "")
    if action not in BATCH_JOB_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"\u672a\u77e5\u7684\u4efb\u52a1\u52a8\u4f5c\uff1a{action}",
            details={"allowed": sorted(BATCH_JOB_ACTIONS)},
        )
    job_ids = _job_ids(payload.get("job_ids"))
    # Raw, not pre-checked: `apply_job_batch` bounds whichever argument the
    # action needs, so this path and the form path are validated by one copy.
    return await apply_job_batch(
        deps.download_service(request),
        action,
        job_ids,
        provider=payload.get("provider"),
        priority=payload.get("priority"),
        announce=lambda job_id: _publish(
            request, EVENT_DOWNLOAD, job_id=job_id
        ),
    )


@router.post("/jobs/{job_id}/priority")
async def set_priority(request: Request, job_id: int) -> dict:
    """Move one job up or down its queue.

    Declared before the catch-all below, like `switch-source`: Starlette matches
    routes in declaration order, so `/jobs/5/priority` would otherwise be
    answered by `job_action` with ``action='priority'`` and refused as unknown.
    """
    _guard(request)
    payload = await _body(request)
    priority = _required_priority(payload)
    service = deps.download_service(request)
    try:
        applied = await service.set_job_priority(job_id, priority)
    except Exception as exc:  # noqa: BLE001 - re-raised below when unexpected
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=job_id)
    return {"job_id": job_id, "priority": applied}


@router.post("/jobs/{job_id}/switch-source")
async def switch_source(request: Request, job_id: int) -> dict:
    """Move a job to a different provider.

    Separate from the action table because it takes an argument, and because
    switching source is a routing decision rather than a lifecycle transition.

    Must stay above the catch-all `/jobs/{job_id}/{action}` route: it used to be
    declared after it, which made this endpoint unreachable -- every request
    landed on `job_action`, which does not know the name and answered 400
    ACTION_UNKNOWN.
    """
    _guard(request)
    payload = await _body(request)
    provider = _required_provider(payload)
    service = deps.download_service(request)
    try:
        new_job_id = await service.switch_source(job_id, provider)
    except Exception as exc:  # noqa: BLE001 - re-raised below when unexpected
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=new_job_id)
    return {"job_id": new_job_id, "provider": provider}


@router.post("/jobs/{job_id}/{action}")
async def job_action(request: Request, job_id: int, action: str) -> dict:
    """Run one lifecycle action against a download or packaging task.

    Declared last on purpose. Its path pattern also matches `/jobs/5/priority`
    and `/jobs/5/switch-source`, and Starlette takes the first route that
    matches, so anything more specific has to be above it.
    """
    _guard(request)
    method_name = JOB_ACTIONS.get(action)
    if method_name is None:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"\u672a\u77e5\u7684\u4efb\u52a1\u52a8\u4f5c\uff1a{action}",
            details={"allowed": sorted(JOB_ACTIONS)},
        )
    service = deps.download_service(request)
    try:
        state = await getattr(service, method_name)(job_id)
    except Exception as exc:  # noqa: BLE001 - re-raised below when unexpected
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=job_id)
    return {"job_id": job_id, "action": action, "state": state}


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
    operator = _guard(request)
    payload = await _body(request)
    fields = payload.get("fields")
    locks = payload.get("locks")
    if fields is not None and not isinstance(fields, dict):
        raise ApiError("FIELDS_INVALID", "fields \u5fc5\u987b\u662f\u5b57\u6bb5\u5230\u503c\u7684\u6620\u5c04")
    if locks is not None and not isinstance(locks, dict):
        raise ApiError("LOCKS_INVALID", "locks \u5fc5\u987b\u662f\u5b57\u6bb5\u5230\u5e03\u5c14\u503c\u7684\u6620\u5c04")
    fields = fields or {}
    locks = locks or {}
    if not fields and not locks:
        raise ApiError(
            "FIELDS_REQUIRED", "\u8bf7\u63d0\u4f9b\u8981\u4fee\u6539\u7684\u5b57\u6bb5"
        )
    unknown = sorted((set(fields) | set(locks)) - set(METADATA_FIELDS))
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

    _publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return {
        "candidate_id": candidate_id,
        "updated": sorted(fields),
        "locked": sorted(name for name, on in locks.items() if on),
        "unlocked": sorted(name for name, on in locks.items() if not on),
        "metadata": await database.effective_metadata(candidate_id),
    }


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
        return ApiError(str(code), str(message), status_code=status)
    return exc


__all__ = [
    "BATCH_JOB_ACTIONS",
    "JOB_ACTIONS",
    "JOB_ACTION_PRIORITY",
    "JOB_ACTION_SWITCH_SOURCE",
    "MAX_BATCH",
    "apply_job_batch",
    "router",
]
