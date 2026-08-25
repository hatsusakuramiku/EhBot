"""Workbench summary.

One request backs the whole dashboard. It is assembled defensively: the torrent
and Telegraph services are optional, and the connection manager only exists once
lifespan startup has run, so a missing piece degrades that section rather than
failing the page. A dashboard that 503s because seeding is switched off would be
worse than one that omits the seeding count.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api import deps
from app.api.serializers import connection_snapshot, job_summary
from app.api.status import is_live


router = APIRouter(tags=["summary"])

#: How many attention-needing items the dashboard lists inline before it stops
#: and defers to the full queue view.
ATTENTION_LIMIT = 5


@router.get("/summary")
async def get_summary(request: Request) -> dict:
    """Counts, connection health and the items asking for a decision."""
    deps.require_session(request)
    database = deps.database(request)
    counts = await database.candidate_counts()

    download = deps.optional_service(request, "download_service")
    downloads: tuple = ()
    packing: tuple = ()
    if download is not None:
        downloads = await download.list_active_jobs()
        packing = await download.list_active_pack_jobs()

    # What the operator actually has to act on: a failure, a stalled torrent, or
    # a packaging task waiting for a password or a missing volume. Ordinary
    # in-flight work is deliberately excluded -- it needs no decision.
    attention = [
        job
        for job in (*downloads, *packing)
        if job.error_code
        or job.is_waiting_for_peers
        or job.state
        in {"CONVERSION_WAITING_PASSWORD", "CONVERSION_WAITING_VOLUMES"}
    ]

    manager = deps.optional_service(request, "connection_manager")
    connections = (
        connection_snapshot(manager.snapshot()) if manager is not None else None
    )

    return {
        "candidates": counts,
        "activity": {
            "downloads": len(downloads),
            "packing": len(packing),
            "live": any(is_live(job.state) for job in (*downloads, *packing)),
        },
        "attention": {
            "total": len(attention),
            "items": [
                job_summary(job) for job in attention[:ATTENTION_LIMIT]
            ],
        },
        "connections": connections,
        "startup_errors": list(
            getattr(request.app.state, "startup_errors", []) or []
        ),
    }


__all__ = ["ATTENTION_LIMIT", "router"]
