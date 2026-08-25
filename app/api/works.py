"""Unified work detail endpoint.

A candidate, an in-flight download and a shelved book are the same work at
different stages, so there is one detail payload for all of them rather than a
separate shape per stage. The interface renders one page and varies only the
action area, which is what stops the operator from having to learn three
layouts for one object.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError
from app.api.serializers import (
    job_summary,
    metadata_entry,
    review_action,
    status_payload,
)
from app.review.models import split_metadata_entries


router = APIRouter(tags=["works"])


@router.get("/works/{candidate_id}")
async def get_work(request: Request, candidate_id: int) -> dict:
    """Detail, metadata, audit timeline and related tasks for one work."""
    deps.require_session(request)
    database = deps.database(request)
    candidate = await database.get_candidate(candidate_id)
    if candidate is None:
        raise ApiError(
            "CANDIDATE_NOT_FOUND",
            "\u5019\u9009\u4e0d\u5b58\u5728\u6216\u5df2\u88ab\u5220\u9664",
            status_code=404,
        )

    entries = await database.list_metadata(candidate_id)
    # Translated values and upstream originals are split server-side using the
    # same helper the old page used, so the two never interleave into a list
    # showing every field twice.
    primary, raw = split_metadata_entries(entries)
    actions = await database.list_review_actions(candidate_id)

    download = deps.optional_service(request, "download_service")
    jobs = (
        await download.list_jobs_for_candidate(candidate_id)
        if download is not None
        else ()
    )

    return {
        "candidate_id": candidate.candidate_id,
        "status": status_payload(candidate.status),
        "title": candidate.title,
        "filter_result": candidate.filter_result,
        "filter_reason": candidate.filter_reason,
        "ex_gid": candidate.ex_gid,
        "ex_gallery_token": candidate.ex_gallery_token,
        "preview_url": candidate.preview_url,
        # `None` means gdata has not answered yet, which is a different thing
        # from a gallery genuinely having no torrent; the interface needs to
        # tell「未查询」from「确认无种」.
        "torrent_count": candidate.torrent_count,
        "torrent_hash": candidate.torrent_hash,
        "messages": [
            {
                "chat_title": message.chat_title,
                "message_id": message.message_id,
                "message_text": message.message_text,
                "attachments": list(message.attachments),
                "message_date": message.message_date,
            }
            for message in candidate.messages
        ],
        "metadata": [metadata_entry(entry) for entry in primary],
        "raw_metadata": [metadata_entry(entry) for entry in raw],
        "timeline": [review_action(entry) for entry in actions],
        "jobs": [job_summary(job) for job in jobs],
    }


__all__ = ["router"]
