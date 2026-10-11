"""Hand-fed links: an ExHentai gallery URL (R63).

The ingest helper creates the candidate already approved and routes it to the
same download queue the review pipeline uses, so a manual add is not a second
path into the system -- only a second way to reach the first one.

Since R63 the form takes an ExHentai gallery link and nothing else. A pasted
link is a claim that a work exists, and the only way to check a claim is to ask
the gallery: the route fetches before it inserts, so a link that does not
resolve -- a deleted gallery, a login-walled one, an outage -- leaves no
candidate behind. A magnet used to be accepted here too; that is gone, because
a magnet has no gallery and therefore no metadata, which is the whole reason
this page exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.candidates import manual as manual_ingest
from app.db.database import DuplicateCandidateError, DuplicateGalleryCandidate
from app.exhentai.service import ExHentaiDownloadError
from app.review.service import ReviewError
from app.web import deps

router = APIRouter()

#: Re-exported for the template context. The wording and the gate live in
#: `app.candidates.manual`, which the JSON API reads too -- one copy, so the page
#: and the API cannot disagree about when a paste is acceptable.
NOT_CONFIGURED_MESSAGE = manual_ingest.NOT_CONFIGURED_MESSAGE


def _exhentai_configured(request: Request) -> bool:
    """Whether an ExHentai connection is configured (see the shared module)."""
    return manual_ingest.exhentai_configured(
        getattr(request.app.state, "connection_manager", None)
    )


async def _ingest_manual_link(request: Request, raw: str) -> int:
    """The shared ingest, with this layer's services.

    The sequence itself (shape, duplicate, gallery) lives in
    `app.candidates.manual` so the JSON API runs the identical code.
    """
    return await manual_ingest.ingest_manual_link(
        raw,
        database=deps.database(request),
        exhentai_service=deps.exhentai_service(request),
        orchestrator=deps.review_orchestrator(request),
        download_service=deps.download_service(request),
    )


async def _render_manual_add(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    existing_candidate_id: int | None = None,
):
    """Render the form, with the two outcome blocks it can carry.

    A duplicate resolves to a card naming the existing work -- id, and the name
    the page shows for it -- rather than to a red line, because the interesting
    thing is not that the paste was refused but which work it already is.
    """
    context: dict[str, object] = {
        "csrf_token": request.session["csrf_token"],
        "exhentai_configured": _exhentai_configured(request),
        "error": error,
        "not_configured_message": NOT_CONFIGURED_MESSAGE,
        "existing_candidate_id": existing_candidate_id,
        "existing_candidate_title": None,
    }
    if existing_candidate_id is not None:
        existing = await deps.database(request).get_candidate(
            existing_candidate_id
        )
        if existing is not None:
            context["existing_candidate_title"] = existing.display_title
    return deps.templates(request).TemplateResponse(
        request=request,
        name="manual_add.html",
        context=context,
        status_code=status_code,
    )


@router.get("/manual-add")
async def manual_add_page(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_manual_add(request)


@router.post("/manual-add")
async def manual_add_submit(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    raw = str(form.get("input") or "").strip()
    # Checked before the link is even looked at, so a deployment with no
    # connection is told what to fix rather than that its link is unreadable.
    if not _exhentai_configured(request):
        return await _render_manual_add(
            request, error=NOT_CONFIGURED_MESSAGE, status_code=400
        )
    try:
        candidate_id = await _ingest_manual_link(request, raw)
    except DuplicateCandidateError as exc:
        # Not an error to fix: the work is already here. Stop at the form and
        # offer the existing candidate, rather than redirecting, so the paste
        # visibly did nothing.
        return await _render_manual_add(
            request,
            existing_candidate_id=exc.existing_candidate_id,
            status_code=409,
        )
    except (ReviewError, ExHentaiDownloadError) as exc:
        return await _render_manual_add(
            request,
            error=str(getattr(exc, "public_message", exc)),
            status_code=400,
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )
