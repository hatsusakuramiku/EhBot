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

from typing import Any

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError
from app.api.events import EVENT_CANDIDATE, EVENT_DOWNLOAD
from app.api.serializers import job_summary
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


def _candidate_ids(raw: Any) -> list[int]:
    """Validate and de-duplicate the requested candidate ids."""
    if not isinstance(raw, list) or not raw:
        raise ApiError(
            "CANDIDATE_IDS_REQUIRED", "\u8bf7\u81f3\u5c11\u9009\u62e9\u4e00\u4e2a\u5019\u9009"
        )
    try:
        ids = list(dict.fromkeys(int(value) for value in raw))
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "CANDIDATE_IDS_INVALID", "\u5019\u9009\u7f16\u53f7\u65e0\u6548"
        ) from exc
    if len(ids) > MAX_BATCH:
        raise ApiError(
            "BATCH_TOO_LARGE",
            f"\u5355\u6b21\u6700\u591a\u5904\u7406 {MAX_BATCH} \u4e2a\u5019\u9009",
            details={"max": MAX_BATCH, "received": len(ids)},
        )
    return ids


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ApiError("BODY_INVALID", "\u8bf7\u6c42\u4f53\u4e0d\u662f\u5408\u6cd5 JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "\u8bf7\u6c42\u4f53\u5fc5\u987b\u662f\u5bf9\u8c61")
    return payload


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


@router.post("/jobs/{job_id}/{action}")
async def job_action(request: Request, job_id: int, action: str) -> dict:
    """Run one lifecycle action against a download or packaging task."""
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


@router.post("/jobs/{job_id}/switch-source")
async def switch_source(request: Request, job_id: int) -> dict:
    """Move a job to a different provider.

    Separate from the action table because it is the only one taking an
    argument, and because switching source is a routing decision rather than a
    lifecycle transition.
    """
    _guard(request)
    payload = await _body(request)
    provider = str(payload.get("provider") or "")
    if not provider:
        raise ApiError("PROVIDER_REQUIRED", "\u8bf7\u6307\u5b9a\u76ee\u6807\u4e0b\u8f7d\u6765\u6e90")
    service = deps.download_service(request)
    try:
        new_job_id = await service.switch_source(job_id, provider)
    except Exception as exc:  # noqa: BLE001 - re-raised below when unexpected
        raise _translate(exc) from exc
    _publish(request, EVENT_DOWNLOAD, job_id=new_job_id)
    return {"job_id": new_job_id, "provider": provider}


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


__all__ = ["JOB_ACTIONS", "MAX_BATCH", "router"]
