"""The one detail page, for a work at any stage of the pipeline.

`render_review_error` lives here rather than with the candidate actions that
raise: a refused action re-renders this page, and putting the renderer next to
the page it renders is what stops a second assembly of the same context from
appearing beside it.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.events import EVENT_DOWNLOAD
from app.api.works import (
    configured_sources,
    effective_library_path,
    work_snapshot,
)
from app.review.models import METADATA_FIELDS, field_label
from app.web import deps

router = APIRouter()


async def render_work(
    request: Request,
    candidate_id: int,
    error: str | None = None,
    message: str | None = None,
    status_code: int = 200,
):
    """The one detail page, for a work at any stage.

    Everything on it comes from `work_snapshot`, the same dict
    `GET /api/v1/works/{id}` returns, so the page cannot offer an action the
    API would refuse. The error path renders this same page rather than a
    stripped-down variant: an operator whose approval was refused needs the
    timeline and the metadata in front of them to decide what to do next.
    """
    snapshot = await work_snapshot(
        deps.database(request),
        candidate_id,
        download=deps.download_service(request),
        sources=configured_sources(request),
        library_path=await effective_library_path(request),
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return deps.templates(request).TemplateResponse(
        request=request,
        name="work_detail.html",
        context={
            "csrf_token": request.session["csrf_token"],
            "work": snapshot,
            "error": error,
            "message": message,
            "metadata_fields": METADATA_FIELDS,
            "field_label": field_label,
            "current_user": request.session.get("username", "admin"),
        },
        status_code=status_code,
    )


@router.post("/works/{candidate_id}/archive-path")
async def save_archive_path(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    directory: str = Form(default=""),
    filename: str = Form(default=""),
    repack: str | None = Form(default=None),
):
    """Set where this work's CBZ belongs, and optionally repack it there now.

    On this page rather than on `/downloaded` because a path is specific to one
    book, and this is the one detail page a work has at every stage. It is a new
    write route on `/works/{id}` -- the first -- and that is deliberate: unlike
    approve, which already had a home under `/candidates/{id}`, there is no
    existing endpoint that sets an archive path, so routing it through one would
    have meant inventing a second meaning for a candidate action.

    A refusal re-renders this page with the reason on it rather than redirecting
    with a query parameter, because the operator has a form open and needs to see
    which value was rejected while they fix it. `repack` is a checkbox, hence
    `str | None`: an unchecked box sends nothing, so not repacking is what
    absence means.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    service = deps.archived_work_service(request)
    try:
        result = await service.set_archive_path(
            candidate_id,
            directory=directory,
            filename=filename,
            operator_name=str(request.session.get("username") or "admin"),
        )
        if repack is not None:
            await deps.conversion_service(request).enqueue_for_candidate(
                candidate_id
            )
    except Exception as exc:  # noqa: BLE001 - domain refusals carry a message
        message = getattr(exc, "public_message", None)
        if message is None:
            raise
        return await render_work(
            request, candidate_id, error=str(message), status_code=400
        )
    request.app.state.event_bus.publish(
        EVENT_DOWNLOAD, candidate_id=candidate_id
    )
    notice = f"归档路径已设为 {result['relative_path']}"
    if result["moved"]:
        notice = f"已移动到 {result['relative_path']}"
    elif repack is not None:
        notice = f"{notice}，重新打包后生效"
    return RedirectResponse(
        f"/works/{candidate_id}?message={quote_plus(notice)}",
        status_code=303,
    )


@router.get("/works/{candidate_id}")
async def work_detail(
    request: Request,
    candidate_id: int,
    error: str | None = None,
    message: str | None = None,
):
    """The unified detail page: 候选期, 下载期 and 入库期 at one URL.

    R6 replaced a 307 to `/candidates/{id}` with the page itself, and turned
    that path around into the redirect. `/works/{id}` is what
    `candidate_summary` has handed every client since R5, and what a work
    keeps being called after it stops being a candidate.

    `error` arrives in the query string because a redirect is the only way a
    form post can report a refusal it could not render itself -- a job action
    that came back here via `return_to`.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await render_work(
        request, candidate_id, error=error, message=message
    )


async def render_review_error(
    request: Request, candidate_id: int, message: str
):
    """A refused action re-renders the detail page with the reason on it.

    This is one call into `_render_work` rather than a second assembly of the
    same context: R5's lesson was that two renderings of one page drift, and
    an operator reading「无法通过」needs the timeline that explains why, not a
    reduced page that only carries the message.
    """
    return await render_work(
        request, candidate_id, error=message, status_code=400
    )
