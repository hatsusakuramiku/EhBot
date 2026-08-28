"""Hand-fed links: an ExHentai gallery URL or a magnet.

The ingest helpers create the candidate already approved and route it to the same
download queue the review pipeline uses, so a manual add is not a second path
into the system -- only a second way to reach the first one.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.candidates.links import find_gallery_ref
from app.downloads.models import PROVIDER_EH_TORRENT, PROVIDER_TELEGRAPH
from app.downloads.service import DownloadError
from app.exhentai.service import ExHentaiDownloadError
from app.review.service import ReviewError
from app.web import deps

router = APIRouter()


async def _exhentai_configured(request: Request) -> bool:
    manager = request.app.state.connection_manager
    if manager is None:
        return False
    try:
        return bool(manager.snapshot().exhentai.configured)
    except Exception:  # noqa: BLE001 - status is a hint, not a gate
        return False


async def _torrent_configured(request: Request) -> bool:
    service = request.app.state.archive_settings_service
    if service is None:
        return False
    try:
        return bool(
            (await service.torrent_client_view()).get("configured")
        )
    except Exception:  # noqa: BLE001 - status is a hint, not a gate
        return False


_MAGNET_PATTERN = re.compile(
    r"magnet:\?.*?xt=urn:btih:([A-Fa-f0-9]{32,40})", re.IGNORECASE
)


async def _ingest_manual_link(request: Request, raw: str) -> int:
    """Turn a guitar link or magnet into an approved, queued candidate."""
    text = raw.strip()
    if not text:
        raise ReviewError("INVALID_LINK", "请输入 ExHentai 画廊链接或磁力链接")
    gallery_ref = find_gallery_ref((text,), text)
    magnet_match = _MAGNET_PATTERN.search(text)
    if gallery_ref is None and magnet_match is None:
        raise ReviewError("INVALID_LINK", "无法识别链接：仅支持 ExHentai 画廊或磁力链接")

    if gallery_ref is not None:
        return await _ingest_manual_eh(request, *gallery_ref)
    return await _ingest_manual_magnet(request, magnet_match.group(1), text)


async def _ingest_manual_eh(request: Request, gid: int, token: str) -> int:
    candidate_id = await deps.database(request).create_manual_candidate(
        filter_reason="手动添加：ExHentai 画廊链接",
        ex_gid=gid,
        ex_gallery_token=token,
        title=f"ExHentai #{gid}",
    )
    try:
        await deps.exhentai_service(request).fetch_metadata_for_candidate(candidate_id)
    except ExHentaiDownloadError as exc:
        # Metadata missing is not fatal: the candidate is already approved
        # and reviewable, and the operator can re-fetch or edit by hand.
        logging.getLogger(__name__).warning(
            "manual_add_metadata_failed candidate=%d error=%s",
            candidate_id,
            exc.public_message,
        )
    await _enqueue_manual_candidate(request, candidate_id)
    return candidate_id


async def _ingest_manual_magnet(
    request: Request, btih: str, raw: str
) -> int:
    torrent_cfg = await deps.archive_settings_service(request).torrent_client()
    if not torrent_cfg.is_configured:
        raise ReviewError(
            "TORRENT_CLIENT_NOT_CONFIG",
            "磁力链接需要已配置 qBittorrent（归档设置）",
        )
    candidate_id = await deps.database(request).create_manual_candidate(
        filter_reason="手动添加：磁力链接",
        magnet_url=raw,
        torrent_hash=btih.lower(),
        title=f"磁力 #{btih[:8]}",
    )
    # A magnet has no gallery to fetch metadata from; the torrent's own
    # DHT metadata arrives as qBittorrent fetches it.
    await deps.download_service(request).enqueue_torrent_download(candidate_id)
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


@router.get("/manual-add")
async def manual_add_page(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return deps.templates(request).TemplateResponse(
        request=request,
        name="manual_add.html",
        context={
            "csrf_token": request.session["csrf_token"],
            "exhentai_configured": await _exhentai_configured(request),
            "torrent_configured": await _torrent_configured(request),
            "error": None,
            "success": None,
        },
    )


@router.post("/manual-add")
async def manual_add_submit(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    raw = str(form.get("input") or "").strip()
    try:
        candidate_id = await _ingest_manual_link(request, raw)
    except (ReviewError, ExHentaiDownloadError) as exc:
        return deps.templates(request).TemplateResponse(
            request=request,
            name="manual_add.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "exhentai_configured": await _exhentai_configured(request),
                "torrent_configured": await _torrent_configured(request),
                "error": str(getattr(exc, "public_message", exc)),
                "success": None,
            },
            status_code=400,
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )
