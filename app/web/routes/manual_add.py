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

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.candidates.links import find_gallery_ref, find_preview_gallery_ref
from app.db.database import DuplicateCandidateError, DuplicateGalleryCandidate
from app.downloads.models import PROVIDER_EH_TORRENT, PROVIDER_TELEGRAPH
from app.downloads.service import DownloadError
from app.exhentai.service import ExHentaiDownloadError
from app.review.service import ReviewError
from app.web import deps

router = APIRouter()

#: Stated once because two places show it: the page's disabled-button notice and
#: the 400 a submit with no connection gets.
NOT_CONFIGURED_MESSAGE = (
    "未配置 Exhentai 连接，请先在「连接」页配置后再手动添加"
)


async def _exhentai_configured(request: Request) -> bool:
    """Whether an ExHentai connection is configured.

    R63 promoted this from a hint to a gate. The page renders it as a notice and
    a disabled button, and the submit path refuses without it -- a deployment
    with no Cookie cannot fetch the gallery that validates the link, so
    accepting the paste would only fail later, somewhere the operator is not
    looking.
    """
    manager = request.app.state.connection_manager
    if manager is None:
        return False
    try:
        return bool(manager.snapshot().exhentai.configured)
    except Exception:  # noqa: BLE001 - a status we cannot read is not a yes
        return False


def _gallery_reference(raw: str) -> tuple[int, str] | None:
    """The `(gid, token)` a pasted link names, or None if it names nothing.

    Both an explicit gallery URL and a Telegraph preview slug are accepted: the
    preview page's slug carries the same id and token (R60), and an operator who
    has only the preview link should not have to reconstruct the gallery URL.
    """
    text = raw.strip()
    if not text:
        return None
    return find_gallery_ref((text,), text) or find_preview_gallery_ref((text,))


async def _ingest_manual_link(request: Request, raw: str) -> int:
    """Validate a pasted gallery link and turn it into an approved candidate.

    The order is the design (R63): shape, then duplicate, then the gallery.
    Shape first because a malformed link is a typo, not a lookup. Duplicate
    second because a re-paste should not cost a scrape, and because the answer
    to "I already added this" is the existing candidate rather than an error.
    The gallery last, and before the insert, because the fetch is what proves
    the link resolves -- see the module docstring.
    """
    text = raw.strip()
    if not text:
        raise ReviewError("INVALID_LINK", "请输入 ExHentai 画廊链接")
    gallery_ref = _gallery_reference(text)
    if gallery_ref is None:
        raise ReviewError(
            "INVALID_LINK",
            "仅支持 ExHentai 画廊链接（exhentai.org / e-hentai.org）",
        )
    gid, token = gallery_ref
    database = deps.database(request)
    existing = await database.candidate_id_for_gallery(gid)
    if existing is not None:
        raise DuplicateGalleryCandidate(existing)
    service = deps.exhentai_service(request)
    # Raises `ExHentaiDownloadError` for a gallery that is gone, needs a login
    # or is unreachable -- the route turns that into a 400 and no row exists.
    metadata, gallery = await service.fetch_metadata_for_gallery(gid, token)
    candidate_id = await database.create_manual_candidate(
        filter_reason="手动添加：ExHentai 画廊链接",
        ex_gid=gid,
        ex_gallery_token=token,
    )
    # Persist exactly the gallery that was validated: a second fetch here would
    # be another request to a host that already answered.
    await service.persist_gallery_metadata(candidate_id, metadata, gallery)
    await _enqueue_manual_candidate(request, candidate_id)
    return candidate_id


async def _enqueue_manual_candidate(
    request: Request, candidate_id: int
) -> None:
    """Queue the best available source for a manually-added candidate.

    The candidate already sits in APPROVED, so the normal approval status
    check is skipped; routing otherwise matches the review pipeline.
    """
    candidate = await deps.database(request).get_candidate(candidate_id)
    if candidate is None:
        return
    provider = deps.review_orchestrator(request).route_source(candidate).provider
    if provider is None:
        logging.getLogger(__name__).info(
            "manual_add_no_source candidate=%d", candidate_id
        )
        return
    try:
        if provider == PROVIDER_EH_TORRENT:
            await deps.download_service(request).enqueue_torrent_download(candidate_id)
        elif provider == PROVIDER_TELEGRAPH:
            await deps.download_service(request).enqueue_telegraph_download(candidate_id)
        else:
            await deps.download_service(request).enqueue_exhentai_download(candidate_id)
    except DownloadError as exc:
        logging.getLogger(__name__).warning(
            "manual_add_enqueue_failed candidate=%d error=%s",
            candidate_id,
            exc.public_message,
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
        "exhentai_configured": await _exhentai_configured(request),
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
    if not await _exhentai_configured(request):
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
