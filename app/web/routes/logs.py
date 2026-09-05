"""The 运行日志 page.

A page of its own rather than a tab of 设置 for two reasons. The first is what it
is for: 设置 is where a deployment is configured, and this is where it is
observed -- an operator watching a download fail is not in the middle of changing
a setting. The second is that it is the only page in the application whose
content arrives continuously, so it owns a stream subscription and a pause
control that no settings tab should have to think about.

The 系统 tab keeps its own read-only tail. That is not a duplicate left behind:
the tab's panel is the file on disk beside the retention settings that produced
it, answering 「文件日志开着吗、写到哪、最后几条长什么样」, while this page is the
live view. They read one snapshot builder each and never share a URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.logs import log_snapshot
from app.web import deps

router = APIRouter()


@router.get("/logs")
async def logs_page(request: Request):
    """Render the tail, then let `logs.js` take over.

    The first page of records is rendered server-side rather than fetched: with
    JavaScript off this page is still a working log viewer with a level filter,
    which is the same rule every other page here follows. What JavaScript adds is
    the live part.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    snapshot = await log_snapshot(request)
    return deps.templates(request).TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "csrf_token": request.session["csrf_token"],
            # Nested under one key so the template, this route and
            # `/api/v1/logs` name the fields identically -- the page reads
            # `logs.entries` and so does the JSON caller.
            "logs": snapshot,
        },
    )
