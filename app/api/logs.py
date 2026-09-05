"""The 运行日志 domain: a live tail with a level floor.

Two sources, one entry shape. `app/logs/broker.py` holds the newest records this
process emitted, and `app/logs/reader.py` reads the file on disk. The buffer is
preferred because it needs no file -- a deployment with `LOG_TO_FILE=false`, or
one whose data directory turned read-only, is precisely where an operator needs
to see why -- and the file is the fallback that survives a restart, since the
buffer starts empty in a process that has only just come up.

The level selector is a **floor**, not an equality test: choosing 警告 keeps
showing errors. A viewer whose 「警告」 hid the errors would be a filter that
loses evidence, and this page exists for the moment evidence matters.

The selector does **not** change the process's threshold. `LOG_LEVEL` stays a
deployment setting for the reason `AGENTS.md` records: a level that lived in the
interface could not be raised to debug the startup that failed before the
interface came up. So a level below the configured one shows nothing new, and the
page says so rather than pretending it filtered.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api import deps
from app.api.serializers import log_entry_payload
from app.api.status import (
    LOG_LEVEL_STATUS,
    VIEWER_LOG_LEVELS,
    log_level_view,
)
from app.logs.broker import LogBroker, parse_buffered_line
from app.logs.reader import (
    MAX_LIMIT,
    LogEntry,
    clamp_limit,
    passes_min_level,
    read_log_tail,
)


router = APIRouter(tags=["logs"])

#: Records replayed to a browser that subscribes to the stream. Smaller than the
#: page's own limit on purpose: the page renders its history from the snapshot
#: endpoint, and the replay only has to cover the gap between that read and the
#: subscription -- plus whatever a reconnect missed.
STREAM_REPLAY = 50

#: The level the page selects when the operator has not chosen one. INFO rather
#: than 全部 because DEBUG is a firehose nobody asked for, and because INFO is
#: what the deployment default emits, so the first load shows everything there is.
DEFAULT_VIEW_LEVEL = "INFO"


def log_broker(request: Request) -> LogBroker:
    """The process's log buffer, off `app.state`.

    Published by `create_app` rather than imported from `app.logging` here, for
    the same reason every other service is read off state: a route module that
    reached into the logging setup would be importable only in a process that had
    configured it.
    """
    broker = getattr(request.app.state, "log_broker", None)
    if broker is None:  # pragma: no cover - always installed by create_app
        from app.logging import log_broker as process_broker

        broker = process_broker()
        request.app.state.log_broker = broker
    return broker


def resolve_view_level(raw: str | None) -> str:
    """The level floor to apply, defaulting rather than refusing.

    Unknown input becomes the default instead of a 400: this arrives from a
    select element and a stale bookmark, and answering a whole page with an error
    because a query parameter aged badly is worse than showing the default view.
    """
    candidate = (raw or "").strip().upper()
    if candidate in VIEWER_LOG_LEVELS:
        return candidate
    return DEFAULT_VIEW_LEVEL


def buffered_entry(line: str) -> dict[str, Any]:
    """One buffered record in the page's entry shape.

    Routed through `LogEntry` so the buffer and the file cannot produce two
    different shapes for the same record -- the page renders one template for
    both, and a missing key there is a 500 during an incident.
    """
    payload = parse_buffered_line(line)
    entry = LogEntry(
        level=str(payload.get("level") or ""),
        timestamp=str(payload.get("timestamp") or ""),
        logger=str(payload.get("logger") or ""),
        event=str(payload.get("event") or ""),
        request_id=_optional_str(payload.get("request_id")),
        job_id=_optional_int(payload.get("job_id")),
        candidate_id=_optional_int(payload.get("candidate_id")),
        error_code=_optional_str(payload.get("error_code")),
        error_message=_optional_str(payload.get("error_message")),
        exception=_optional_str(payload.get("exception")),
        raw=_optional_str(payload.get("raw")),
    )
    return log_entry_payload(entry)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def level_choices(active: str) -> list[dict[str, Any]]:
    """The level selector, as codes with their resolved labels.

    Built here rather than in the template so the page and the JSON body offer
    the same set, and so no Chinese severity name is written in markup --
    `log_level_view` owns that vocabulary.
    """
    return [
        {
            "code": code,
            "label": LOG_LEVEL_STATUS[code].label,
            "selected": code == active,
        }
        for code in VIEWER_LOG_LEVELS
    ]


async def log_snapshot(request: Request) -> dict[str, Any]:
    """Everything the 运行日志 page renders, newest first.

    The buffer is read first and the file only when the buffer has nothing to
    offer at this level, rather than merging the two: they overlap by definition
    -- the same record is in both -- and de-duplicating formatted lines by
    content would silently collapse two genuinely identical events, which during
    a retry loop is the very pattern being investigated.
    """
    settings = request.app.state.settings
    level = resolve_view_level(request.query_params.get("level"))
    limit = clamp_limit(request.query_params.get("limit"))
    broker = log_broker(request)

    entries = [
        entry
        for entry in (
            buffered_entry(record.line)
            for record in broker.snapshot()
        )
        if passes_min_level(str(entry["level"]["code"]), level)
    ][:limit]
    source = "buffer"
    file_present = False
    if not entries:
        # Nothing in memory at this level: either the process just started, or
        # everything it has logged is below the floor. The file may still hold
        # what the previous process wrote.
        file_entries, file_present = await asyncio.to_thread(
            read_log_tail, settings.log_dir, limit=limit, min_level=level
        )
        if file_entries:
            entries = [log_entry_payload(entry) for entry in file_entries]
            source = "file"
    else:
        file_present = await asyncio.to_thread(_log_file_exists, settings.log_dir)

    return {
        "entries": entries,
        "level": level,
        "levels": level_choices(level),
        "limit": limit,
        "max_limit": MAX_LIMIT,
        "source": source,
        "file_present": file_present,
        "file_enabled": settings.log_to_file,
        # What the process is actually emitting. A floor below this one cannot
        # reveal anything, and the page says so instead of leaving the operator
        # to wonder why 调试 looks identical to 信息.
        "configured_level": settings.log_level,
        "configured_level_label": log_level_view(settings.log_level).label,
        "access_log": settings.log_access,
        "buffered": len(broker.snapshot()),
        "buffer_capacity": broker.capacity,
        "dropped": broker.dropped_count,
        "subscribers": broker.subscriber_count,
        "stream_path": "/api/v1/logs/stream",
    }


def _log_file_exists(log_dir) -> bool:
    return (log_dir / "ehbot.log").exists()


@router.get("/logs")
async def get_logs(request: Request) -> dict[str, Any]:
    """The tail as JSON, for the page's own refresh and for a scripted read."""
    deps.require_session(request)
    return await log_snapshot(request)


@router.get("/logs/stream")
async def stream_logs(request: Request) -> StreamingResponse:
    """Every record as it is logged, filtered client-side by level.

    The filter is applied in the browser rather than per subscriber: the stream
    is one broadcast queue, and a server-side filter would mean a level change
    had to tear down and rebuild the connection -- during which the records it
    was opened to catch are the ones it misses. Frames are cheap; the page keeps
    what it wants.

    Headers match `/api/v1/events`: `no-cache` so a proxy cannot replay a stale
    stream, and `X-Accel-Buffering: no` because nginx would otherwise hold frames
    until its buffer filled, which defeats the whole endpoint.
    """
    deps.require_session(request)
    broker = log_broker(request)
    return StreamingResponse(
        broker.stream(replay=STREAM_REPLAY),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "DEFAULT_VIEW_LEVEL",
    "STREAM_REPLAY",
    "buffered_entry",
    "level_choices",
    "log_broker",
    "log_snapshot",
    "resolve_view_level",
    "router",
]
