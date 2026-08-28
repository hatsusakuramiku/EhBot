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

from collections.abc import Mapping, Sequence

from fastapi import APIRouter, Query, Request

from app.api import deps
from app.api.contracts import ApiError, Page, PageParams
from app.api.serializers import candidate_summary
from app.db.database import CANDIDATE_COUNT_KEYS, CANDIDATE_FACETS


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

#: Selected values allowed in one facet group. A link carrying fifty tags would
#: build fifty EXISTS subqueries for a result that cannot be non-empty; the cap
#: keeps a hand-edited or looping URL from turning a filter into a table scan.
MAX_FACET_VALUES = 8


def candidate_tab_counts(counts: Mapping[str, int]) -> dict[str, int]:
    """Per-tab totals derived from the per-status counts.

    Derived rather than queried: a tab badge and the list under it have to agree,
    and the only way to guarantee that is to add up exactly the statuses
    `CANDIDATE_TABS` selects. `all` uses the table's own total so a candidate in
    a state no tab claims is still counted somewhere.
    """
    tallies: dict[str, int] = {}
    for tab, statuses in CANDIDATE_TABS.items():
        if not statuses:
            tallies[tab] = int(counts.get("total", 0))
            continue
        tallies[tab] = sum(
            int(counts.get(CANDIDATE_COUNT_KEYS[status], 0))
            for status in statuses
        )
    return tallies


def candidate_facet_selection(
    raw: Mapping[str, Sequence[str] | None],
) -> dict[str, tuple[str, ...]]:
    """Clean a query string's facet values into the database layer's shape.

    Blank values are dropped and duplicates collapse, because a checkbox form
    posts an empty control and a re-submitted URL repeats what is already
    selected -- neither should widen or narrow the result.
    """
    selection: dict[str, tuple[str, ...]] = {}
    for name in CANDIDATE_FACETS:
        values: list[str] = []
        for value in raw.get(name) or ():
            token = value.strip()
            if token and token not in values:
                values.append(token)
        if not values:
            continue
        if len(values) > MAX_FACET_VALUES:
            raise ApiError(
                "FACET_TOO_MANY",
                f"\u7b5b\u9009\u9879\u201c{name}\u201d\u6700\u591a\u9009"
                f"{MAX_FACET_VALUES}\u4e2a\u503c",
                details={"facet": name, "limit": MAX_FACET_VALUES},
            )
        selection[name] = tuple(values)
    return selection


@router.get("/candidates")
async def list_candidates(
    request: Request,
    tab: str = Query("pending"),
    search: str | None = Query(None),
    sort: str = Query("newest"),
    tags: list[str] | None = Query(None),
    artist: list[str] | None = Query(None),
    language: list[str] | None = Query(None),
    category: list[str] | None = Query(None),
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
    facets = candidate_facet_selection(
        {
            "tags": tags,
            "artist": artist,
            "language": language,
            "category": category,
        }
    )
    database = deps.database(request)
    params = PageParams.clamp(page, page_size)
    items, total = await database.list_candidates_page(
        statuses=CANDIDATE_TABS[tab],
        search=search,
        facets=facets,
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
    counts = await database.candidate_counts()
    payload["counts"] = counts
    payload["tab_counts"] = candidate_tab_counts(counts)
    payload["tab"] = tab
    payload["sort"] = sort
    payload["search"] = search or ""
    payload["filters"] = {name: list(values) for name, values in facets.items()}
    return payload


@router.get("/candidates/facets")
async def list_candidate_facets(
    request: Request, tab: str = Query("pending")
) -> dict:
    """Filter values available in one tab, most common first."""
    deps.require_session(request)
    if tab not in CANDIDATE_TABS:
        raise ApiError(
            "TAB_UNKNOWN",
            f"\u672a\u77e5\u7684\u5019\u9009\u5206\u7ec4\uff1a{tab}",
            details={"allowed": sorted(CANDIDATE_TABS)},
        )
    database = deps.database(request)
    options = await database.candidate_facets(statuses=CANDIDATE_TABS[tab])
    return {
        "tab": tab,
        "facets": {
            name: [
                {"value": value, "count": count} for value, count in values
            ]
            for name, values in options.items()
        },
    }


__all__ = [
    "CANDIDATE_SORTS",
    "CANDIDATE_TABS",
    "MAX_FACET_VALUES",
    "candidate_facet_selection",
    "candidate_tab_counts",
    "router",
]
