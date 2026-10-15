"""Shared safe operation reads and versioned ingestion retries."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.auth import _json_body
from app.api.contracts import ApiError
from app.api.events import EVENT_CANDIDATE
from app.api.status import INGEST_OPERATION_ERRORS, ingest_operation_payload
from app.candidates.entries import IngestError
from app.candidates.operations import get_operation, retry_operation


router = APIRouter(tags=['ingestion'])


def operation_error(exc):
    code = exc.code
    status = (404 if code.endswith('NOT_FOUND') else
              428 if code in {'PRECONDITION_REQUIRED', 'IDEMPOTENCY_KEY_REQUIRED'} else 409)
    return ApiError(code, INGEST_OPERATION_ERRORS.get(code, '变更未能受理，请刷新后核对。'), status_code=status)


async def retry_ingest_operation(request, operation_id, payload):
    """Command application shared by the JSON API and the form adapter."""
    if set(payload) - {'expected_revision', 'idempotency_key'}:
        raise ApiError('INGEST_COMMAND_INVALID', '请求包含不支持的字段。')
    try:
        result = await asyncio.to_thread(
            retry_operation, deps.database(request), operation_id,
            expected_revision=payload.get('expected_revision'), idempotency_key=payload.get('idempotency_key'),
        )
    except IngestError as exc:
        raise operation_error(exc) from None
    bus = getattr(request.app.state, 'event_bus', None)
    if bus is not None:
        bus.publish(EVENT_CANDIDATE, candidate_id=result['candidate_id'])
    return ingest_operation_payload(result)


@router.get('/ingest-operations/{operation_id}')
async def api_ingest_operation(request: Request, operation_id: str):
    await deps.require_session(request)
    try:
        result = await asyncio.to_thread(get_operation, deps.database(request), operation_id)
    except IngestError as exc:
        raise operation_error(exc) from None
    return JSONResponse(ingest_operation_payload(result), headers={'Cache-Control': 'no-store'})


@router.post('/ingest-operations/{operation_id}/retry')
async def api_ingest_retry(request: Request, operation_id: str):
    await deps.require_session(request)
    deps.require_csrf(request)
    result = await retry_ingest_operation(request, operation_id, await _json_body(request))
    return JSONResponse(result, status_code=202, headers={'Cache-Control': 'no-store'})
