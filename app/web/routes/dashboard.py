"""The workbench at `/`."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.activity import queue_snapshot
from app.api.candidates import candidate_tab_counts
from app.api.status import system_health_view
from app.web import deps

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    #: The needs-attention roll-up is computed from the same snapshot the
    #: activity page renders, not from a second query: the workbench and the
    #: queue must never disagree about how many tasks are waiting on the
    #: operator, and the number on the workbench is the one that decides
    #: whether they go and look.
    snapshot = await queue_snapshot(deps.download_service(request))
    return deps.templates(request).TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "csrf_token": request.session["csrf_token"],
            #: Resolved here rather than registered as a Jinja global: the
            #: startup-error list is per-process state read off `app.state`,
            #: and a global would have to reach for the request to find it.
            #: Same input as `/readyz`, so the badge and the probe cannot
            #: disagree.
            "health": system_health_view(
                getattr(request.app.state, "startup_errors", None)
            ),
            "connections": deps.connection_manager(request).snapshot(),
            # Tallied by tab rather than by raw status, so a metric here and
            # the tab strip on `/candidates` can never show two different
            # numbers for the same queue.
            "candidate_counts": candidate_tab_counts(
                await deps.database(request).candidate_counts()
            ),
            "attention": snapshot["attention"],
        },
    )
