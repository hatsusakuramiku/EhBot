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
import asyncio
from uuid import uuid4

from app.candidates.entries import IngestError, claim_manual_gallery, fail_claim, renew_claim
from app.candidates.links import find_gallery_ref, find_preview_gallery_ref
from app.db.database import DuplicateGalleryCandidate
from app.downloads.models import PROVIDER_EH_TORRENT, PROVIDER_TELEGRAPH, PROVIDER_TELEGRAM, PROVIDER_TELEGRAM_USER
from app.downloads.service import DownloadError
from app.downloads.execution import run_sync
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
    def reserve():
        with database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return claim_manual_gallery(connection, gid, owner=uuid4().hex,
                                         active_entry_ids=database.ingest_processors.active_ids())
    try:
        async with database.ingest_processors.lock:
            claim = await run_sync(reserve)
            processor = asyncio.current_task()
            database.ingest_processors.tasks[claim.entry_id] = processor
    except IngestError as exc:
        raise ReviewError(exc.code, "该画廊已有摄取正在处理，请稍后核对结果") from exc
    finished = asyncio.Event()
    publication = asyncio.Lock()
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_renew_manual_claim(database, claim, finished, publication))
            try:
                # Validation and its writes retain the borrowed EH account;
                # only the one key owner may perform the supplier request.
                async with exhentai_service.account_request():
                    candidate_id = await _ingest_validated_gallery(gid, token, database=database,
                        exhentai_service=exhentai_service, orchestrator=orchestrator,
                        download_service=download_service, claim=claim, finished=finished, publication=publication)
            finally:
                finished.set()
    except BaseExceptionGroup as exc:
        # Keep existing domain exceptions visible to Web/API translations.
        await run_sync(_release_manual_claim, database, claim)
        if len(exc.exceptions) == 1:
            error = exc.exceptions[0]
            if isinstance(error, IngestError):
                raise ReviewError(error.code, "画廊处理已被更新或撤销，请核对当前候选") from None
            raise error from None
        raise
    except BaseException:
        await run_sync(_release_manual_claim, database, claim)
        raise
    finally:
        if database.ingest_processors.tasks.get(claim.entry_id) is processor:
            del database.ingest_processors.tasks[claim.entry_id]
    return candidate_id


async def _renew_manual_claim(database, claim, finished, publication):
    while not finished.is_set():
        try:
            await asyncio.wait_for(finished.wait(), 30)
        except TimeoutError:
            async with publication:
                if finished.is_set():
                    return
                def renew():
                    with database.connection() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        renew_claim(connection, claim)
                await run_sync(renew)


def _release_manual_claim(database, claim):
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            fail_claim(connection, claim, error_code="MANUAL_GALLERY_VALIDATION_FAILED")
        except IngestError as exc:
            if exc.code != "INGEST_STALE_PROCESSOR":
                raise


async def _ingest_validated_gallery(gid, token, *, database, exhentai_service,
                                    orchestrator, download_service, claim, finished, publication):
    # Raises for a gallery that is gone, needs a login or is unreachable.
    metadata, gallery = await exhentai_service.fetch_metadata_for_gallery(
        gid, token
    )
    # Persist exactly the gallery that was validated: a second fetch here would
    # be another request to a host that already answered.
    async with publication:
        candidate_id = await exhentai_service.publish_manual_gallery(gid, token, metadata, gallery, ingest_claim=claim)
        finished.set()
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
    routed = orchestrator.route_source(candidate)
    provider = routed.provider
    if provider is None:
        logging.getLogger(__name__).info(
            "manual_add_no_source candidate=%d", candidate_id
        )
        return
    try:
        if provider == PROVIDER_TELEGRAM:
            await download_service.enqueue_telegram_download(candidate_id, routed.attachment or {})
        elif provider == PROVIDER_TELEGRAM_USER:
            await download_service.enqueue_telegram_user_download(candidate_id, routed.attachment or {})
        elif provider == PROVIDER_EH_TORRENT:
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
