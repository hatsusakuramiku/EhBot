"""Activity domain: queue, packaging and history.

These three lists were previously one page that reloaded itself with
`<meta http-equiv="refresh">`, which threw away scroll position and any open
menu every few seconds. They are separated here because they answer different
questions -- what is running, what is being packaged, what already finished --
and because only the first two ever change on their own.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api import deps
from app.api.contracts import MAX_PAGE_SIZE
from app.api.serializers import job_summary
from app.api.status import is_live


router = APIRouter(tags=["activity"])


@router.get("/queue")
async def get_queue(request: Request) -> dict:
    """Live snapshot of downloads and packaging tasks.

    ``live`` tells the interface whether anything is still advancing on its own.
    When it is false the client can stop polling entirely, which is what keeps
    an idle tab from waking the process every two seconds.
    """
    deps.require_session(request)
    service = deps.download_service(request)
    downloads = await service.list_active_jobs()
    packing = await service.list_active_pack_jobs()
    return {
        "downloads": [job_summary(job) for job in downloads],
        "packing": [job_summary(job) for job in packing],
        "counts": {
            "downloads": len(downloads),
            "packing": len(packing),
        },
        "live": any(
            is_live(job.state) for job in (*downloads, *packing)
        ),
    }


@router.get("/history")
async def get_history(
    request: Request,
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
) -> dict:
    """Finished tasks, newest first.

    Terminal rows are never deleted, so history is a query rather than a
    separate archive table.
    """
    deps.require_session(request)
    service = deps.download_service(request)
    jobs = await service.list_history_jobs(limit=limit)
    return {
        "items": [job_summary(job) for job in jobs],
        "total": len(jobs),
        "limit": limit,
    }


__all__ = ["router"]
