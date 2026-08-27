"""Activity domain: queue, packaging and history.

These three lists were previously one page that reloaded itself with
`<meta http-equiv="refresh">`, which threw away scroll position and any open
menu every few seconds. They are separated here because they answer different
questions -- what is running, what is being packaged, what already finished --
and because only the first two ever change on their own.

`queue_snapshot` is the one assembler: the JSON endpoint returns it directly and
the HTML page renders the same dict. Grouping and the needs-attention roll-up
are computed here rather than in the browser, so the section headings, their
counts and the banner at the top of the page can never disagree with each other.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api import deps
from app.api.contracts import MAX_PAGE_SIZE
from app.api.serializers import job_summary, queue_group_payload
from app.api.status import ATTENTION_STATUS, attention_view, is_live
from app.downloads.models import QUEUE_GROUPS


router = APIRouter(tags=["activity"])


def group_jobs(jobs) -> list[dict[str, Any]]:
    """Split one queue into its sections, in display order.

    Empty sections are dropped rather than rendered as an empty heading: an
    operator reading「需干预 0」has to stop and check that it really is zero,
    where an absent section says the same thing without asking anything of them.
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        group: [] for group in QUEUE_GROUPS
    }
    for job in jobs:
        buckets[job.queue_group].append(job_summary(job))
    return [
        queue_group_payload(group, buckets[group])
        for group in QUEUE_GROUPS
        if buckets[group]
    ]


def attention_summary(jobs) -> dict[str, Any]:
    """Roll up everything waiting on the operator, by reason.

    Reasons keep the declaration order of `ATTENTION_STATUS` -- the specific
    asks (a missing volume, a missing password) before the generic failure --
    so the banner reads as a to-do list rather than an unsorted pile. Job ids
    travel with each reason so the dashboard can link straight into the queue
    without fetching it first.
    """
    found: dict[str, list[int]] = {}
    for job in jobs:
        reason = job.attention_reason
        if reason is None:
            continue
        found.setdefault(reason, []).append(job.job_id)
    ordered = [reason for reason in ATTENTION_STATUS if reason in found]
    # A reason the DTO invented but the vocabulary has not caught up with is
    # still shown, appended, rather than silently dropped from the count.
    ordered += [reason for reason in found if reason not in ATTENTION_STATUS]
    return {
        "total": sum(len(ids) for ids in found.values()),
        "reasons": [
            {
                "reason": attention_view(reason).to_payload(),
                "count": len(found[reason]),
                "job_ids": found[reason],
            }
            for reason in ordered
        ],
    }


async def queue_snapshot(service) -> dict[str, Any]:
    """Everything the activity queue and packaging tabs need, in one read.

    Both queues are fetched here so ``live`` and the attention roll-up cover
    them together: a packaging job waiting on a password is as much a reason to
    show the banner as a stalled download is.
    """
    downloads = await service.list_active_jobs()
    packing = await service.list_active_pack_jobs()
    return {
        "downloads": group_jobs(downloads),
        "packing": group_jobs(packing),
        "counts": {
            "downloads": len(downloads),
            "packing": len(packing),
        },
        "attention": attention_summary((*downloads, *packing)),
        # False stops the client polling entirely. Read from the states rather
        # than from the groups so a stalled torrent still counts: it sits under
        # 需干预, but only another request can reveal that a peer appeared.
        "live": any(is_live(job.state) for job in (*downloads, *packing)),
    }


@router.get("/queue")
async def get_queue(request: Request) -> dict:
    """Live snapshot of downloads and packaging tasks.

    ``live`` tells the interface whether anything is still advancing on its own.
    When it is false the client can stop polling entirely, which is what keeps
    an idle tab from waking the process every two seconds.
    """
    deps.require_session(request)
    return await queue_snapshot(deps.download_service(request))


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


__all__ = [
    "attention_summary",
    "group_jobs",
    "queue_snapshot",
    "router",
]
