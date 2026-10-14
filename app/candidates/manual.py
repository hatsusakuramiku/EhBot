"""Shared manual-add ingest, used by the page and the JSON API.

The order is the design (R63): shape, then duplicate, then the gallery. Shape
first because a malformed link is a typo, not a lookup. Duplicate second because
a re-paste should not cost a scrape, and because the answer to 「我已经加过这个」
is the existing candidate rather than an error. The gallery last, and before the
insert, because the fetch is what proves the link resolves.

Deliberately free of `Request`: the page layer and the JSON layer pass the same
services in, so a link the page refuses cannot be accepted by the API. Two
sequences would drift, and the drift would be invisible until an operator pasted
the one link that lands on the wrong side of it.
"""

from __future__ import annotations

import logging

from app.candidates.links import find_gallery_ref, find_preview_gallery_ref
from app.db.database import DuplicateGalleryCandidate
from app.downloads.models import PROVIDER_EH_TORRENT, PROVIDER_TELEGRAPH
from app.downloads.service import DownloadError
from app.review.service import ReviewError

#: Stated once because two places show it: the page's disabled-button notice,
#: the 400 a page submit with no connection gets, and the JSON API's refusal.
NOT_CONFIGURED_MESSAGE = "未配置 Exhentai 连接或账号尚未启用，请先保存、测试并启用账号后再手动添加"


def exhentai_configured(manager) -> bool:
    """Whether an ExHentai connection is configured.

    R63 promoted this from a hint to a gate. A deployment with no Cookie cannot
    fetch the gallery that validates the link, so accepting the paste would only
    fail later, somewhere the operator is not looking.
    """
    if manager is None:
        return False
    try:
        return manager.snapshot().exhentai.state == "connected"
    except Exception:  # noqa: BLE001 - a status we cannot read is not a yes
        return False


def gallery_reference(raw: str) -> tuple[int, str] | None:
    """The `(gid, token)` a pasted link names, or None if it names nothing.

    Both an explicit gallery URL and a Telegraph preview slug are accepted: the
    preview page's slug carries the same id and token (R60), and an operator who
    has only the preview link should not have to reconstruct the gallery URL.
    """
    text = raw.strip()
    if not text:
        return None
    return find_gallery_ref((text,), text) or find_preview_gallery_ref((text,))


async def ingest_manual_link(
    raw: str,
    *,
    database,
    exhentai_service,
    orchestrator,
    download_service,
) -> int:
    """Validate a pasted gallery link and turn it into an approved candidate.

    Raises `ReviewError` for a shape the service will not accept,
    `DuplicateGalleryCandidate` when the gallery is already here, and
    `ExHentaiDownloadError` when the gallery cannot be read -- in every failing
    case no row is created, so a refusal leaves the database as it was.
    """
    text = raw.strip()
    if not text:
        raise ReviewError("INVALID_LINK", "请输入 ExHentai 画廊链接")
    reference = gallery_reference(text)
    if reference is None:
        raise ReviewError(
            "INVALID_LINK",
            "仅支持 ExHentai 画廊链接（exhentai.org / e-hentai.org）",
        )
    gid, token = reference
    existing = await database.candidate_id_for_gallery(gid)
    if existing is not None:
        raise DuplicateGalleryCandidate(existing)
    # The caller's candidate/metadata writes belong to the same borrowed
    # account as validation. A stop cannot finish while they are pending.
    async with exhentai_service.account_request():
        return await _ingest_validated_gallery(gid, token, database=database,
            exhentai_service=exhentai_service, orchestrator=orchestrator,
            download_service=download_service)


async def _ingest_validated_gallery(gid, token, *, database, exhentai_service,
                                    orchestrator, download_service):
    # Raises for a gallery that is gone, needs a login or is unreachable.
    metadata, gallery = await exhentai_service.fetch_metadata_for_gallery(
        gid, token
    )
    candidate_id = await database.create_manual_candidate(
        filter_reason="手动添加：ExHentai 画廊链接",
        ex_gid=gid,
        ex_gallery_token=token,
    )
    # Persist exactly the gallery that was validated: a second fetch here would
    # be another request to a host that already answered.
    await exhentai_service.persist_gallery_metadata(
        candidate_id, metadata, gallery
    )
    await enqueue_manual_candidate(
        candidate_id,
        database=database,
        orchestrator=orchestrator,
        download_service=download_service,
    )
    return candidate_id


async def enqueue_manual_candidate(
    candidate_id: int, *, database, orchestrator, download_service
) -> None:
    """Queue the best available source for a manually-added candidate.

    The candidate already sits in APPROVED, so the normal approval status check
    is skipped; routing otherwise matches the review pipeline.
    """
    candidate = await database.get_candidate(candidate_id)
    if candidate is None:
        return
    provider = orchestrator.route_source(candidate).provider
    if provider is None:
        logging.getLogger(__name__).info(
            "manual_add_no_source candidate=%d", candidate_id
        )
        return
    try:
        if provider == PROVIDER_EH_TORRENT:
            await download_service.enqueue_torrent_download(candidate_id)
        elif provider == PROVIDER_TELEGRAPH:
            await download_service.enqueue_telegraph_download(candidate_id)
        else:
            await download_service.enqueue_exhentai_download(candidate_id)
    except DownloadError as exc:
        logging.getLogger(__name__).warning(
            "manual_add_enqueue_failed candidate=%d error=%s",
            candidate_id,
            exc.public_message,
        )
