"""`/api/v1` router.

This module owns the vocabulary endpoint and the event stream, and mounts the
per-domain routers. Domain routes live in their own modules so a section can be
read without scrolling past the others; they are included here rather than
registered on the app directly so the `/api/v1` prefix and the tag set are
declared once.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api import deps
from app.api.actions import router as actions_router
from app.api.activity import router as activity_router
from app.api.candidates import router as candidates_router
from app.api.events import EventBus
from app.api.summary import router as summary_router
from app.api.thumbnails import router as thumbnails_router
from app.api.works import router as works_router
from app.api.status import (
    CANDIDATE_STATUS,
    CONNECTION_STATUS,
    CONVERSION_STATUS,
    DOWNLOAD_STATUS,
    PROVIDER_STATUS,
)


router = APIRouter(prefix="/api/v1", tags=["api"])

# Read-only domains first, then the state-changing routes. Order is irrelevant
# to matching here -- no two of these declare the same path -- but grouping them
# this way keeps the read/write split visible at a glance.
router.include_router(summary_router)
router.include_router(candidates_router)
router.include_router(works_router)
router.include_router(activity_router)
router.include_router(thumbnails_router)
router.include_router(actions_router)


def _event_bus(request: Request) -> EventBus:
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:  # pragma: no cover - always installed by create_app
        bus = EventBus()
        request.app.state.event_bus = bus
    return bus


@router.get("/meta")
async def api_meta(request: Request) -> dict:
    """Vocabulary and client tuning values.

    Serving the status registry means the interface renders labels and tones
    from the same table Python uses, so a new backend state cannot show up in
    one place as Chinese and in another as a raw enum.
    """
    deps.require_session(request)
    settings = request.app.state.settings
    return {
        "statuses": {
            "candidate": {
                code: view.to_payload()
                for code, view in CANDIDATE_STATUS.items()
            },
            "download": {
                code: view.to_payload()
                for code, view in DOWNLOAD_STATUS.items()
            },
            "conversion": {
                code: view.to_payload()
                for code, view in CONVERSION_STATUS.items()
            },
            "provider": {
                code: view.to_payload()
                for code, view in PROVIDER_STATUS.items()
            },
            "connection": {
                code: view.to_payload()
                for code, view in CONNECTION_STATUS.items()
            },
        },
        "features": {
            # The interface hides a source it cannot use rather than offering a
            # button that always fails.
            "telegraph": bool(settings.telegraph_enabled),
            "torrent": bool(settings.torrent_enabled),
            "tag_translation": bool(settings.tag_translation_enabled),
        },
        "polling": {
            # Visible-tab interval. The stream is the primary signal; polling
            # is the fallback for a proxy that buffers SSE.
            "interval_ms": 2000,
            "idle_interval_ms": 15000,
        },
    }


@router.get("/events")
async def api_events(request: Request) -> StreamingResponse:
    """Server-sent events for state transitions.

    Headers matter here: `no-cache` stops a proxy from replaying a stale
    stream, and `X-Accel-Buffering: no` disables nginx's response buffering,
    which would otherwise hold frames until the buffer filled and defeat the
    entire point of the endpoint.
    """
    deps.require_session(request)
    bus = _event_bus(request)
    return StreamingResponse(
        bus.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events/stats")
async def api_event_stats(request: Request) -> dict:
    """Subscriber and drop counters, for diagnosing a stuck interface."""
    deps.require_session(request)
    bus = _event_bus(request)
    return {
        "subscribers": bus.subscriber_count,
        "dropped": bus.dropped_count,
    }


__all__ = ["router"]