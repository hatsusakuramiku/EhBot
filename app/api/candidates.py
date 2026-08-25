"""Candidate domain endpoints.

The review queue is the highest-traffic surface, so this is the one list that
has to page, filter and sort in SQL rather than in Python: the old page read a
fixed 100 rows per status and had no way to reach row 101.

Tabs are named here rather than in the template because the same grouping backs
both the tab bar and the count badges. A tab that maps to several statuses (for
example「待补充」covering NEEDS_INFO and NEEDS_REVISION) must select and count
the same set, which is only guaranteed while one table drives both.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api import deps
from app.api.contracts import ApiError, Page, PageParams
from app.api.serializers import candidate_summary


router = APIRouter(tags=["candidates"])


#: Tab key -> candidate statuses. `all` maps to no filter at all rather than to
#: the union of the others, so a candidate in a state not yet given a tab still
#: appears somewhere instead of vanishing from the interface.
CANDIDATE_TABS: dict[str, tuple[str, ...]] = {
    "all": (),
    "pending": ("PENDING_REVIEW",),
    "needs_info": ("NEEDS_INFO", "NEEDS_REVISION"),
    "approved": ("APPROVED", "PROCESSING", "DOWNLOADED"),
    "rejected": ("REJECTED",),
    "failed": ("FAILED",),
}

#: Sort keys the database layer accepts. Repeated here so an invalid value is
#: rejected at the edge with a clear message, instead of silently falling back
#: and leaving the operator wondering why their ordering was ignored.
CANDIDATE_SORTS: frozenset[str] = frozenset(
    {"newest", "oldest", "updated", "title"}
)


@router.get("/candidates")
async def list_candidates(
    request: Request,
    tab: str = Query("pending"),
    search: str | None = Query(None),
    sort: str = Query("newest"),
    page: int | None = Query(None),
    page_size: int | None = Query(None),
) -> dict:
    """A page of candidates for the review grid."""
    deps.require_session(request)
    if tab not in CANDIDATE_TABS:
        raise ApiError(
            "TAB_UNKNOWN",
            f"\u672a\u77e5\u7684\u5019\u9009\u5206\u7ec4\uff1a{tab}",
            details={"allowed": sorted(CANDIDATE_TABS)},
        )
    if sort not in CANDIDATE_SORTS:
        raise ApiError(
            "SORT_UNKNOWN",
            f"\u672a\u77e5\u7684\u6392\u5e8f\u65b9\u5f0f\uff1a{sort}",
            details={"allowed": sorted(CANDIDATE_SORTS)},
        )
    database = deps.database(request)
    params = PageParams.clamp(page, page_size)
    items, total = await database.list_candidates_page(
        statuses=CANDIDATE_TABS[tab],
        search=search,
        sort=sort,
        offset=params.offset,
        limit=params.limit,
    )
    payload = Page.of(
        [candidate_summary(item) for item in items], total, params
    ).to_payload()
    # Counts accompany the list so the tab badges refresh in the same round
    # trip that refreshes the grid; fetching them separately is how a badge
    # ends up disagreeing with the list beneath it.
    payload["counts"] = await database.candidate_counts()
    payload["tab"] = tab
    payload["sort"] = sort
    payload["search"] = search or ""
    return payload


__all__ = ["CANDIDATE_SORTS", "CANDIDATE_TABS", "router"]
