import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import quote_plus
import hmac
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import time

from fastapi import FastAPI, Form, HTTPException, Request
import httpx
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from starlette.middleware.sessions import SessionMiddleware

from app.candidates.ingestor import CandidateIngestor
from app.candidates.links import find_gallery_ref
from app.auto_approval.rules import (
    RuleValidationError,
    render_rule_dsl,
    validate_rule_ast,
)
from app.auto_approval.service import AutomaticApprovalService
from app.archive.service import (
    LIMIT_KEYS as ARCHIVE_LIMIT_KEYS,
    ArchiveSettingsError,
    ArchiveSettingsService,
)
from app.config import Settings
from app.bootstrap import (
    format_bootstrap_banner,
    remove_bootstrap_password,
    write_bootstrap_password,
)
from app.connections.exhentai import ExHentaiCredentials
from app.connections.manager import ConnectionManager
from app.connections.models import ProviderConnectionError
from app.db.database import Database
from app.api.actions import (
    BATCH_JOB_ACTIONS,
    JOB_ACTIONS,
    REVIEW_BATCH_ACTIONS,
    apply_job_batch,
    apply_review_batch,
)
from app.api.activity import queue_snapshot
from app.api.candidates import (
    CANDIDATE_SORTS,
    CANDIDATE_TABS,
    candidate_facet_selection,
    candidate_tab_counts,
)
from app.api.contracts import ApiError, PageParams, api_error_handler
from app.api.events import (
    EVENT_CANDIDATE,
    EVENT_CONVERSION,
    EVENT_DOWNLOAD,
    EventBus,
)
from app.api.serializers import candidate_summary, job_summary
from app.api.status import (
    candidate_tab_view,
    connection_view,
    metadata_source_view,
    provider_label,
    status_label,
    status_tone,
    status_view,
)
from app.api.v1 import router as api_v1_router
from app.api.works import configured_sources, work_snapshot
from app.errors import AppError, app_error_handler
from app.logging import configure_logging

from app.review.models import (
    METADATA_FIELDS,
    REVIEWABLE_STATUSES,
    field_label,
    split_metadata_entries,
)
from app.review.orchestration import ReviewOrchestrator
from app.review.service import ReviewError, ReviewService
from app.downloads.models import (
    DEFAULT_JOB_PRIORITY,
    MAX_JOB_PRIORITY,
    MIN_JOB_PRIORITY,
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAPH,
)
from app.downloads.service import DownloadError, DownloadService
from app.conversion.service import (
    ConversionError,
    ConversionService,
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_RUNNING,
)
from app.exhentai.service import ExHentaiDownloadError, ExHentaiService
from app.exhentai.tagdb import TagTranslator
from app.telegraph.fetcher import FetchLimits
from app.telegraph.guard import check_image_url
from app.telegraph.models import TelegraphError
from app.telegraph.service import TelegraphService
from app.thumbnails.service import ThumbnailService
from app.torrent.models import TorrentError
from app.torrent.service import TorrentService
from app.web.routes import shell_context, ui_kit_context
from app.exhentai.tagdb_sync import TagDatabaseError, TagDatabaseSync
from app.secrets import SecretStore
from app.session_secret import resolve_session_secret
from app.storage.readiness import ensure_writable_directory



async def _load_tag_translator(data_path, client):
    """Synchronize and index the EhTagTranslation database.

    Returns None when no translation data is available so metadata ingestion
    keeps working with untranslated English tags.
    """
    sync = TagDatabaseSync(data_path, client)
    reason = "cache_only"
    try:
        reason = (await sync.synchronize()).reason
    except TagDatabaseError as exc:
        logging.getLogger(__name__).warning(
            "tag_database_unavailable", extra={"error_code": exc.code}
        )
        return None
    payload = await asyncio.to_thread(sync.load_cached)
    if payload is None:
        return None
    translator = TagTranslator()
    await asyncio.to_thread(translator.load, payload)
    logging.getLogger(__name__).info(
        "tag_database_ready version=%s entries=%d reason=%s",
        translator.version,
        translator.entry_count,
        reason,
    )
    return translator

def local_return_to(raw: str | None) -> str | None:
    """Accept a same-site path to come back to, or nothing.

    Job actions live at `/activity/jobs/{id}/...` because the queue page owned
    them first, but R6 lets the work detail page post the same forms, and an
    operator who paused a download from `/works/12` must not land on the queue.
    The page says where it wants to return in a hidden field, which makes this
    an open-redirect surface: an absolute URL here would let a crafted form send
    someone off-site with the app's own redirect. So only a rooted path is
    honoured -- no scheme, no `//host` (protocol-relative, which browsers do
    treat as another origin), no backslash (which some parsers normalise into
    one), and no control characters.
    """
    target = (raw or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(char < " " or char == "\x7f" for char in target):
        return None
    return target


def create_app(
    settings: Settings | None = None,
    *,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
    exhentai_transport: httpx.AsyncBaseTransport | None = None,
    tagdb_transport: httpx.AsyncBaseTransport | None = None,
    telegraph_transport: httpx.AsyncBaseTransport | None = None,
    telegraph_resolver=None,
    torrent_client_transport: httpx.AsyncBaseTransport | None = None,
    thumbnail_transport: httpx.AsyncBaseTransport | None = None,
    thumbnail_resolver=None,
) -> FastAPI:
    configure_logging()
    app_settings = settings or Settings.from_env()
    database = Database(app_settings.data_path / "ehbot.db")
    password_hasher = PasswordHash.recommended()
    # Generated and persisted on first start when not configured, so a fresh
    # deployment needs no hand-created secret file to come up.
    session_secret = resolve_session_secret(
        app_settings.data_path, app_settings.app_secret_key
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        app.state.startup_errors = app_settings.readiness_errors()
        if session_secret.error:
            app.state.startup_errors.append(session_secret.error)
        connection_manager: ConnectionManager | None = None
        telegram_client: httpx.AsyncClient | None = None
        exhentai_client: httpx.AsyncClient | None = None
        tagdb_client: httpx.AsyncClient | None = None
        telegraph_client: httpx.AsyncClient | None = None
        torrent_client: httpx.AsyncClient | None = None
        thumbnail_client: httpx.AsyncClient | None = None
        try:
            for path in (
                app_settings.data_path,
                app_settings.library_path,
                app_settings.work_path,
            ):
                ensure_writable_directory(path)
            await database.initialize()
            admin_auth = await database.get_admin_auth("admin")
            if admin_auth is None or not admin_auth[1]:
                bootstrap_password = secrets.token_urlsafe(18)
                bootstrap_hash = await asyncio.to_thread(
                    password_hasher.hash, bootstrap_password
                )
                await database.set_bootstrap_admin("admin", bootstrap_hash)
                password_file = await asyncio.to_thread(
                    write_bootstrap_password,
                    app_settings.data_path,
                    bootstrap_password,
                )
                print(
                    format_bootstrap_banner(bootstrap_password, password_file),
                    flush=True,
                )
                logging.getLogger(__name__).warning(
                    "bootstrap_admin_password_created path=%s", password_file
                )
            else:
                await asyncio.to_thread(
                    remove_bootstrap_password, app_settings.data_path
                )
            secret_store = SecretStore(app_settings.data_path / "private")
            telegram_client = httpx.AsyncClient(
                base_url="https://api.telegram.org",
                timeout=40,
                transport=telegram_transport,
            )
            exhentai_client = httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                transport=exhentai_transport,
            )
            connection_manager = ConnectionManager(
                secret_store,
                database,
                telegram_client=telegram_client,
                exhentai_client=exhentai_client,
                candidate_ingestor=CandidateIngestor(database),
            )
            application.state.connection_manager = connection_manager
            await connection_manager.start()
            archive_settings_service = ArchiveSettingsService(
                database,
                app_settings.data_path,
                default_library_path=app_settings.library_path,
                default_work_path=app_settings.work_path,
                default_torrent_category=app_settings.torrent_category,
                default_torrent_keep_seeding=(
                    app_settings.torrent_keep_seeding
                ),
            )
            if app_settings.telegraph_enabled:
                # A dedicated client with no cookies or auth headers: the page
                # images come from third-party hosts that must never see a
                # credential belonging to this deployment.
                telegraph_client = httpx.AsyncClient(
                    timeout=60,
                    follow_redirects=False,
                    transport=telegraph_transport,
                )
                application.state.telegraph_service = TelegraphService(
                    database,
                    app_settings.work_path,
                    http_client=telegraph_client,
                    limits=FetchLimits(
                        concurrency=app_settings.telegraph_concurrency,
                        max_images=app_settings.telegraph_max_images,
                        max_image_bytes=(
                            app_settings.telegraph_max_image_bytes
                        ),
                        max_total_bytes=(
                            app_settings.telegraph_max_total_bytes
                        ),
                        timeout_seconds=float(
                            app_settings.telegraph_timeout_seconds
                        ),
                    ),
                    require_filecount_match=(
                        app_settings.telegraph_require_filecount_match
                    ),
                    work_path_provider=archive_settings_service.work_path,
                    resolver=telegraph_resolver,
                )
            if app_settings.torrent_enabled:
                # qBittorrent is a local service on a private network, so this
                # client follows redirects and keeps its own SID cookie; the
                # ExHentai client is reused for the `.torrent` fetch because
                # that request needs the gallery Cookie.
                torrent_client = httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                    transport=torrent_client_transport,
                )
                application.state.torrent_service = TorrentService(
                    database,
                    app_settings.work_path,
                    config_provider=(
                        archive_settings_service.torrent_client
                    ),
                    credentials_provider=(
                        lambda: _build_exhentai_credentials(secret_store)
                    ),
                    http_client=exhentai_client,
                    client_http_client=torrent_client,
                    poll_seconds=float(app_settings.torrent_poll_seconds),
                    work_path_provider=archive_settings_service.work_path,
                    # Read off app.state per delivery: the conversion service
                    # is constructed further down in this same scope, so the
                    # name is not bound yet and the module-level accessor is
                    # shadowed by that local. This also means a settings change
                    # applies without a restart.
                    auto_pack=(
                        lambda candidate_id: (
                            application.state.conversion_service
                            .enqueue_for_candidate(candidate_id)
                        )
                    ),
                )
            download_service = DownloadService(
                database,
                app_settings.work_path,
                telegram_client_factory=(
                    lambda: _build_telegram_context(
                        secret_store, telegram_client
                    )
                ),
                exhentai_download=(
                    lambda candidate_id: application.state.exhentai_service
                    .download_archive_for_candidate(candidate_id)
                ),
                telegraph_download=(
                    lambda candidate_id: telegraph_service()
                    .download_for_candidate(candidate_id)
                ),
                torrent_push=(
                    lambda candidate_id: torrent_service()
                    .push_for_candidate(candidate_id)
                ),
                torrent_abandon=(
                    lambda job_id: torrent_service().abandon(job_id)
                ),
                torrent_verify=(
                    lambda job_id: torrent_service().complete_if_ready(job_id)
                ),
                # Auto-pack a finished download into a CBZ. The idempotent
                # conversion insert makes the torrent route's own auto-pack a
                # no-op here, so a single conversion job is created per
                # candidate no matter how many completions fire.
                auto_pack=(
                    lambda candidate_id: (
                        application.state.conversion_service
                        .enqueue_for_candidate(candidate_id)
                    )
                ),
                auto_pack_enabled=(
                    archive_settings_service.auto_pack_after_download
                ),
                work_path_provider=archive_settings_service.work_path,
                # Publishing carries ids only: the browser answers by calling
                # the REST endpoint, so the stream never becomes a second,
                # possibly stale, source of job state.
                notify=(
                    lambda **data: app.state.event_bus.publish(
                        EVENT_DOWNLOAD, **data
                    )
                ),
            )
            application.state.download_service = download_service
            application.state.archive_settings_service = archive_settings_service
            # Linux and Docker images ship no archiver, so fetch the pinned
            # official 7-Zip build once per version if it is missing.
            if app_settings.archive_toolchain_auto_install:
                await archive_settings_service.ensure_toolchain()
            conversion_service = ConversionService(
                database,
                app_settings.work_path,
                app_settings.library_path,
                settings_service=archive_settings_service,
                data_path=app_settings.data_path,
                # The packaging queue publishes on its own channel. Downloads
                # and packaging are two queues in the interface, so a client
                # watching one must not be woken by the other.
                notify=(
                    lambda **data: app.state.event_bus.publish(
                        EVENT_CONVERSION, **data
                    )
                ),
            )
            application.state.conversion_service = conversion_service
            await conversion_service.start()
            tag_translator = None
            if app_settings.tag_translation_enabled:
                tagdb_client = httpx.AsyncClient(
                    timeout=60,
                    follow_redirects=True,
                    transport=tagdb_transport,
                )
                tag_translator = await _load_tag_translator(
                    app_settings.data_path, tagdb_client
                )
            application.state.tag_translator = tag_translator
            exhentai_service = ExHentaiService(
                database,
                app_settings.work_path,
                app_settings.library_path,
                credentials_provider=(
                    lambda: _build_exhentai_credentials(secret_store)
                ),
                http_client=exhentai_client,
                translator=tag_translator,
                work_path_provider=archive_settings_service.work_path,
            )
            application.state.exhentai_service = exhentai_service
            if app_settings.thumbnails_enabled:
                # Covers come from the same image hosts the preview fetcher
                # talks to, so this client carries no cookie either: a cover is
                # public art, and sending the gallery Cookie to a CDN would
                # leak the credential for nothing.
                thumbnail_client = httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                    transport=thumbnail_transport,
                )
                checker = (
                    (lambda url: check_image_url(url, resolver=thumbnail_resolver))
                    if thumbnail_resolver is not None
                    else check_image_url
                )
                application.state.thumbnail_service = ThumbnailService(
                    database,
                    app_settings.data_path / "thumbnails",
                    thumbnail_client,
                    image_url_checker=checker,
                )
            await download_service.start()
            if application.state.torrent_service is not None:
                # Parked jobs are read from the database each pass, so this
                # also re-attaches to whatever the client kept working on
                # while EhBot was down.
                await application.state.torrent_service.start()
        except (OSError, ValueError, sqlite3.Error) as exc:
            app.state.startup_errors.append(str(exc))
            logging.getLogger(__name__).error(
                "application_startup_failed", extra={"error_code": "STARTUP_FAILED"}
            )
        try:
            yield
        finally:
            if connection_manager is not None:
                await connection_manager.stop()
            download_service = application.state.download_service
            if download_service is not None:
                await download_service.stop()
            conversion_service = application.state.conversion_service
            if conversion_service is not None:
                await conversion_service.stop()
            torrent_service_instance = application.state.torrent_service
            if torrent_service_instance is not None:
                await torrent_service_instance.stop()
            if telegram_client is not None:
                await telegram_client.aclose()
            if exhentai_client is not None:
                await exhentai_client.aclose()
            if tagdb_client is not None:
                await tagdb_client.aclose()
            if telegraph_client is not None:
                await telegraph_client.aclose()
            if torrent_client is not None:
                await torrent_client.aclose()
            if thumbnail_client is not None:
                await thumbnail_client.aclose()

    app = FastAPI(
        title="EhBot", lifespan=lifespan, root_path=app_settings.app_root_path
    )
    app.state.settings = app_settings
    app.state.database = database
    app.state.connection_manager = None
    app.state.download_service = None
    app.state.conversion_service = None
    app.state.archive_settings_service = None
    app.state.exhentai_service = None
    app.state.telegraph_service = None
    app.state.torrent_service = None
    app.state.thumbnail_service = None
    app.state.tag_translator = None
    # Fan-out for state transitions. Created eagerly so a worker can publish
    # before any browser has connected (publishing with no subscriber is a
    # no-op, which is what makes it cheap to call from the download loop).
    app.state.event_bus = EventBus()
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(api_v1_router)
    login_attempts: dict[str, tuple[int, float]] = {}
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret.key,
        https_only=app_settings.session_cookie_secure,
        same_site="lax",
    )
    # `shell_context` supplies `nav_items`, `current_path` and `active_domain`
    # to every rendered page. A context processor rather than 25 edited
    # `TemplateResponse` calls: the shell is a property of the response, not of
    # each handler, and a handler that forgets to pass it would render a page
    # with no navigation at all.
    templates = Jinja2Templates(
        directory=Path(__file__).parent / "web" / "templates",
        context_processors=[shell_context],
    )

    # Labels, tones and provider names come from `app.api.status`, so a state
    # reads the same in a template as it does in a JSON response.
    templates.env.filters["status_label"] = status_label
    templates.env.globals["status_label"] = status_label
    templates.env.filters["status_tone"] = status_tone
    templates.env.globals["status_tone"] = status_tone
    templates.env.filters["provider_label"] = provider_label
    templates.env.globals["provider_label"] = provider_label
    # The badge macro takes a whole `StatusView`, not a label and a tone
    # separately, so that a template can never pair one state's label with
    # another's colour.
    templates.env.filters["status_view"] = status_view
    templates.env.globals["status_view"] = status_view
    templates.env.filters["connection_view"] = connection_view
    templates.env.globals["connection_view"] = connection_view
    # Tab names for the workbench metrics, from the same vocabulary the tab
    # strip uses: 待审核 on the dashboard and 待审核 on `/candidates` are one
    # string in `app/api/status.py`.
    templates.env.globals["candidate_tab_view"] = candidate_tab_view
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "web" / "static"),
        name="static",
    )

    def validate_csrf(request: Request, supplied_token: str) -> None:
        expected_token = request.session.get("csrf_token", "")
        if not expected_token or not hmac.compare_digest(
            supplied_token, expected_token
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def require_authenticated(request: Request) -> RedirectResponse | None:
        if not request.session.get("authenticated"):
            return RedirectResponse(
                request.url_for("login_page").path, status_code=303
            )
        if request.session.get("must_change_password"):
            return RedirectResponse(
                request.url_for("change_password_page").path, status_code=303
            )
        return None

    def connection_manager() -> ConnectionManager:
        manager = app.state.connection_manager
        if manager is None:
            raise HTTPException(status_code=503, detail="Connections are unavailable")
        return manager

    def _parse_csv_tags(raw: object) -> tuple[str, ...]:
        if raw is None:
            return ()
        cleaned: list[str] = []
        for item in str(raw).replace("\n", ",").split(","):
            token = item.strip().lower()
            if token:
                cleaned.append(token)
        return tuple(cleaned)

    async def _build_telegram_context(secret_store, default_client):
        token = await asyncio.to_thread(
            secret_store.read, "telegram_bot_token"
        )
        return token, default_client

    def download_service() -> DownloadService:
        service = app.state.download_service
        if service is None:
            raise HTTPException(status_code=503, detail="Downloads are unavailable")
        return service

    def conversion_service() -> ConversionService:
        service = app.state.conversion_service
        if service is None:
            raise HTTPException(status_code=503, detail="Conversion is unavailable")
        return service

    def archive_settings_service() -> ArchiveSettingsService:
        service = app.state.archive_settings_service
        if service is None:
            raise HTTPException(
                status_code=503, detail="Archive settings are unavailable"
            )
        return service

    def exhentai_service() -> ExHentaiService:
        service = app.state.exhentai_service
        if service is None:
            raise HTTPException(status_code=503, detail="ExHentai is unavailable")
        return service

    def telegraph_service() -> TelegraphService:
        service = app.state.telegraph_service
        if service is None:
            raise HTTPException(
                status_code=503, detail="Telegraph source is unavailable"
            )
        return service

    def torrent_service() -> TorrentService:
        service = app.state.torrent_service
        if service is None:
            raise HTTPException(
                status_code=503, detail="Torrent source is unavailable"
            )
        return service

    async def _build_exhentai_credentials(secret_store):
        cookies_json = await asyncio.to_thread(
            secret_store.read, "exhentai_cookies"
        )
        if not cookies_json:
            return None
        try:
            return ExHentaiCredentials.from_json(cookies_json)
        except (ValueError, KeyError):
            return None

    async def _exhentai_configured() -> bool:
        manager = app.state.connection_manager
        if manager is None:
            return False
        try:
            return bool(manager.snapshot().exhentai.configured)
        except Exception:  # noqa: BLE001 - status is a hint, not a gate
            return False

    async def _torrent_configured() -> bool:
        service = app.state.archive_settings_service
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

    async def _ingest_manual_link(raw: str) -> int:
        """Turn a guitar link or magnet into an approved, queued candidate."""
        text = raw.strip()
        if not text:
            raise ReviewError("INVALID_LINK", "请输入 ExHentai 画廊链接或磁力链接")
        gallery_ref = find_gallery_ref((text,), text)
        magnet_match = _MAGNET_PATTERN.search(text)
        if gallery_ref is None and magnet_match is None:
            raise ReviewError("INVALID_LINK", "无法识别链接：仅支持 ExHentai 画廊或磁力链接")

        if gallery_ref is not None:
            return await _ingest_manual_eh(*gallery_ref)
        return await _ingest_manual_magnet(magnet_match.group(1), text)

    async def _ingest_manual_eh(gid: int, token: str) -> int:
        candidate_id = await database.create_manual_candidate(
            filter_reason="手动添加：ExHentai 画廊链接",
            ex_gid=gid,
            ex_gallery_token=token,
            title=f"ExHentai #{gid}",
        )
        try:
            await exhentai_service().fetch_metadata_for_candidate(candidate_id)
        except ExHentaiDownloadError as exc:
            # Metadata missing is not fatal: the candidate is already approved
            # and reviewable, and the operator can re-fetch or edit by hand.
            logging.getLogger(__name__).warning(
                "manual_add_metadata_failed candidate=%d error=%s",
                candidate_id,
                exc.public_message,
            )
        await _enqueue_manual_candidate(candidate_id)
        return candidate_id

    async def _ingest_manual_magnet(btih: str, raw: str) -> int:
        torrent_cfg = await archive_settings_service().torrent_client()
        if not torrent_cfg.is_configured:
            raise ReviewError(
                "TORRENT_CLIENT_NOT_CONFIG",
                "磁力链接需要已配置 qBittorrent（归档设置）",
            )
        candidate_id = await database.create_manual_candidate(
            filter_reason="手动添加：磁力链接",
            magnet_url=raw,
            torrent_hash=btih.lower(),
            title=f"磁力 #{btih[:8]}",
        )
        # A magnet has no gallery to fetch metadata from; the torrent's own
        # DHT metadata arrives as qBittorrent fetches it.
        await download_service().enqueue_torrent_download(candidate_id)
        return candidate_id

    async def _enqueue_manual_candidate(candidate_id: int) -> None:
        """Queue the best available source for a manually-added candidate.

        The candidate already sits in APPROVED, so the normal approval status
        check is skipped; routing otherwise matches the review pipeline.
        """
        candidate = await database.get_candidate(candidate_id)
        if candidate is None:
            return
        provider, attachment = _route_download_source(candidate)
        if provider is None:
            logging.getLogger(__name__).info(
                "manual_add_no_source candidate=%d", candidate_id
            )
            return
        try:
            if provider == PROVIDER_EH_TORRENT:
                await download_service().enqueue_torrent_download(candidate_id)
            elif provider == PROVIDER_TELEGRAPH:
                await download_service().enqueue_telegraph_download(candidate_id)
            else:
                await download_service().enqueue_exhentai_download(candidate_id)
        except DownloadError as exc:
            logging.getLogger(__name__).warning(
                "manual_add_enqueue_failed candidate=%d error=%s",
                candidate_id,
                exc.public_message,
            )

    def review_service() -> ReviewService:
        return ReviewService(database)

    review_orchestrator = ReviewOrchestrator(
        database,
        download_service,
        torrent_available=lambda: app.state.torrent_service is not None,
        telegraph_available=lambda: app.state.telegraph_service is not None,
    )
    # Published so `app/api` reaches the same instance the pages use.
    app.state.review_orchestrator = review_orchestrator

    def _route_download_source(candidate) -> tuple[str | None, dict | None]:
        routed = review_orchestrator.route_source(candidate)
        return routed.provider, routed.attachment

    async def _approve_candidates_and_enqueue(
        candidate_ids: list[int], operator: str
    ) -> tuple[int, ...]:
        return await review_orchestrator.approve_and_enqueue(
            candidate_ids, operator
        )

    async def _apply_automatic_approval(candidate_id: int) -> bool:
        return await review_orchestrator.apply_automatic_approval(candidate_id)

    #: The six candidate tabs, in the order the strip shows them. The label is
    #: *not* here: it comes from `candidate_tab_view`, so a tab name reads the
    #: same in the strip, in a JSON payload and in a badge. What is here is page
    #: copy -- the sentence under the heading and the two lines an empty tab
    #: shows -- which belongs to the page and to nothing else.
    #:
    #: 「全部」 is listed first because it is the superset, but `/candidates`
    #: renders 待审核: the domain's front door should be the queue an operator
    #: opens it to work, the way `/activity` is 队列 rather than a combined view.
    CANDIDATE_PAGE_TABS: tuple[dict[str, str], ...] = (
        {
            "key": "all",
            "href": "/candidates/all",
            "description": "全部候选，包含已结束的记录",
            "empty_title": "还没有任何候选",
            "empty_hint": "白名单来源的新消息与手动添加的链接都会落到这里",
        },
        {
            "key": "pending",
            "href": "/candidates",
            "description": "确认元数据后可批量加入下载队列",
            "empty_title": "暂无待审核候选",
            "empty_hint": "白名单来源的新候选会显示在这里",
        },
        {
            "key": "needs_info",
            "href": "/candidates/needs-info",
            "description": "缺标题、缺附件或需要修订的候选",
            "empty_title": "暂无待补充候选",
            "empty_hint": "信息不足的候选会显示在这里",
        },
        {
            "key": "approved",
            "href": "/candidates/approved",
            "description": "已通过审核、正在下载或已完成的候选",
            "empty_title": "暂无已通过候选",
            "empty_hint": "通过审核的候选会进入下载队列并显示在这里",
        },
        {
            "key": "rejected",
            "href": "/candidates/rejected",
            "description": "已驳回的候选，可在详情页重新入队",
            "empty_title": "暂无驳回记录",
            "empty_hint": "驳回不会删除候选，随时可以改主意",
        },
        {
            "key": "failed",
            "href": "/candidates/failed",
            "description": "下载或打包失败、需要检查后重试的候选",
            "empty_title": "暂无失败候选",
            "empty_hint": "失败的候选会显示在这里，附带失败原因",
        },
    )

    #: Sort keys offered in the toolbar, with the words for each. The keys are
    #: `CANDIDATE_SORTS`; the database owns what each one means in SQL.
    CANDIDATE_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
        ("newest", "最新发现"),
        ("oldest", "最早发现"),
        ("updated", "最近更新"),
        ("title", "按标题"),
    )

    #: Facet name -> sidebar heading. The names are `CANDIDATE_FACETS`, which is
    #: what decides how each one is matched in SQL.
    CANDIDATE_FILTER_GROUPS: tuple[tuple[str, str], ...] = (
        ("tags", "标签"),
        ("artist", "作者"),
        ("language", "语言"),
        ("category", "分类"),
    )

    def _int_param(raw: str | None) -> int | None:
        """A query-string integer, or None when it is absent or junk.

        A hand-edited `?page=abc` should show page one, not a 422: the page is a
        place an operator lands from a bookmark, and nothing about it is worth
        refusing a render for.
        """
        try:
            return int(raw) if raw not in (None, "") else None
        except ValueError:
            return None

    def _query_href(request: Request, **params: object) -> str:
        """This URL with some query parameters replaced.

        A view toggle and a page link each differ from the current page by one
        parameter. Rebuilding the query string in the template would mean
        re-listing every filter at four call sites, and the first one to forget
        `search` would silently drop it; Starlette does the merge instead.

        Returned as a path so the rendered link is relative, which keeps the
        page correct behind a reverse proxy that terminates a different scheme.
        """
        url = request.url.include_query_params(**params)
        return f"{url.path}?{url.query}" if url.query else url.path

    async def _render_candidates(
        request: Request,
        tab: str,
        *,
        error: str | None = None,
        status_code: int = 200,
    ):
        """Render one candidate tab.

        Replaces the four near-identical queue pages. Every tab reads the same
        query string -- search, sort, view, facets, page -- so a filter survives
        a tab switch instead of being a per-page feature, and the tab counts are
        shown on all six because「待审核还有多少」is not a question worth changing
        tab to answer.

        Arguments are read from `request.query_params` rather than declared on
        each route: six routes repeating eight parameters is six chances for one
        of them to drift, and everything here is optional by construction.
        """
        params = request.query_params
        search = (params.get("search") or "").strip()
        sort = params.get("sort") or "newest"
        if sort not in CANDIDATE_SORTS:
            # Forgiving on purpose, unlike the JSON endpoint: a bookmarked link
            # with a sort we have since renamed should still show the list.
            sort = "newest"
        view = params.get("view") if params.get("view") in {"grid", "list"} else "grid"
        try:
            facets = candidate_facet_selection(
                {name: params.getlist(name) for name, _ in CANDIDATE_FILTER_GROUPS}
            )
        except ApiError as exc:
            facets = {}
            error = error or exc.message
        page = PageParams.clamp(
            _int_param(params.get("page")), _int_param(params.get("page_size"))
        )
        statuses = CANDIDATE_TABS[tab]

        if tab == "pending":
            # Kept from the old queue page: opening 待审核 is what enriches new
            # candidates and lets an auto-approval rule fire. Bounded to the
            # page the operator is looking at, which the pre-R5 version was not.
            first, _ = await database.list_candidates_page(
                statuses=statuses,
                search=search,
                facets=facets,
                sort=sort,
                offset=page.offset,
                limit=page.limit,
            )
            await exhentai_service().enrich_candidates_for_review(first)
            for candidate in first:
                await _apply_automatic_approval(candidate.candidate_id)

        items, total = await database.list_candidates_page(
            statuses=statuses,
            search=search,
            facets=facets,
            sort=sort,
            offset=page.offset,
            limit=page.limit,
        )
        counts = candidate_tab_counts(await database.candidate_counts())
        options = await database.candidate_facets(statuses=statuses)
        current = next(
            entry for entry in CANDIDATE_PAGE_TABS if entry["key"] == tab
        )
        # Batch review is offered wherever a candidate can still be reviewed.
        # `REVIEWABLE_STATUSES` decides that, not the tab name, and 「全部」 has
        # no status filter so it always offers it.
        batch_enabled = not statuses or bool(set(statuses) & REVIEWABLE_STATUSES)
        return templates.TemplateResponse(
            request=request,
            name="candidates.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "tab": tab,
                "tab_title": candidate_tab_view(tab).label,
                "tab_description": current["description"],
                # The tab's own path, with no query string: what 「清除」 goes to
                # and what the filter form posts back to.
                "tab_href": current["href"],
                "tabs": [
                    {
                        "key": entry["key"],
                        "href": entry["href"],
                        "label": candidate_tab_view(entry["key"]).label,
                        "count": counts.get(entry["key"], 0),
                    }
                    for entry in CANDIDATE_PAGE_TABS
                ],
                # Serialised through the same function the JSON list uses, so a
                # card and an API client describe a candidate identically -- the
                # cover URL included, which is a proxy path and never upstream.
                "candidates": [candidate_summary(item) for item in items],
                "total": total,
                "page": page.page,
                "page_size": page.page_size,
                # Paging and the view switch are links rather than scripted
                # buttons, so both survive JavaScript being off and a shared URL
                # reopens exactly what the sender was looking at.
                "prev_href": _query_href(request, page=page.page - 1),
                "next_href": _query_href(request, page=page.page + 1),
                "grid_href": _query_href(request, view="grid"),
                "list_href": _query_href(request, view="list"),
                "sort": sort,
                "sorts": [
                    {"key": key, "label": label}
                    for key, label in CANDIDATE_SORT_OPTIONS
                ],
                "search": search,
                "view": view,
                # List-view headers. The select and action columns are dropped
                # where the tab cannot review anything, so a terminal tab does
                # not show an empty checkbox column.
                "columns": [
                    *(
                        [{"key": "select", "label": "选择"}]
                        if batch_enabled
                        else []
                    ),
                    {"key": "candidate", "label": "候选"},
                    {"key": "status", "label": "状态"},
                    {"key": "tags", "label": "标签"},
                    {"key": "messages", "label": "消息", "numeric": True},
                    {"key": "updated", "label": "更新"},
                    {"key": "actions", "label": "操作"},
                ],
                "filters": [
                    {
                        "name": name,
                        "title": title,
                        "options": [
                            {
                                "value": value,
                                "label": value,
                                "count": count,
                                "checked": value in facets.get(name, ()),
                            }
                            for value, count in options.get(name, ())
                        ],
                    }
                    for name, title in CANDIDATE_FILTER_GROUPS
                ],
                "active_filters": sum(
                    len(values) for values in facets.values()
                ),
                # Batch review is offered wherever a candidate can still be
                # reviewed -- see `batch_enabled` above.
                "batch_enabled": batch_enabled,
                "empty_title": current["empty_title"],
                "empty_hint": current["empty_hint"],
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/auto-approval-rules")
    async def auto_approval_rules_page(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="auto_approval_rules.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "rules": await database.list_auto_approval_rules(),
                "preview_ids": (),
                "error": None,
            },
        )

    @app.post("/auto-approval-rules")
    async def save_auto_approval_rule(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        form = await request.form()
        validate_csrf(request, str(form.get("csrf_token") or ""))
        try:
            name = str(form.get("name") or "").strip()
            if not name:
                raise RuleValidationError("规则名称不能为空")
            priority = int(str(form.get("priority") or "100"))
            # Rules are expressed as `Regex({Field}, 'pattern')`. The pattern is
            # validated here (re.compile) so a syntax error can never be saved.
            ast = validate_rule_ast(
                {
                    "kind": "regex",
                    "field": str(form.get("field") or "").strip(),
                    "pattern": str(form.get("pattern") or ""),
                }
            )
            await database.save_auto_approval_rule(
                rule_id=None,
                name=name,
                enabled=form.get("enabled") == "on",
                priority=priority,
                condition=ast,
                dsl_snapshot=render_rule_dsl(ast),
            )
        except (RuleValidationError, ValueError, json.JSONDecodeError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="auto_approval_rules.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "rules": await database.list_auto_approval_rules(),
                    "preview_ids": (),
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse(
            request.url_for("auto_approval_rules_page").path, status_code=303
        )

    @app.post("/auto-approval-rules/{rule_id}/toggle")
    async def toggle_auto_approval_rule(rule_id: int, request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        form = await request.form()
        validate_csrf(request, str(form.get("csrf_token") or ""))
        await database.set_auto_approval_rule_enabled(
            rule_id, form.get("enabled") == "on"
        )
        return RedirectResponse(
            request.url_for("auto_approval_rules_page").path, status_code=303
        )

    @app.post("/auto-approval-rules/{rule_id}/preview")
    async def preview_auto_approval_rule(rule_id: int, request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        form = await request.form()
        validate_csrf(request, str(form.get("csrf_token") or ""))
        rule = await database.get_auto_approval_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return templates.TemplateResponse(
            request=request,
            name="auto_approval_rules.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "rules": await database.list_auto_approval_rules(),
                "preview_ids": await AutomaticApprovalService(database).preview(rule),
                "preview_rule_id": rule_id,
                "error": None,
            },
        )

    @app.get("/")
    async def dashboard(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        #: The needs-attention roll-up is computed from the same snapshot the
        #: activity page renders, not from a second query: the workbench and the
        #: queue must never disagree about how many tasks are waiting on the
        #: operator, and the number on the workbench is the one that decides
        #: whether they go and look.
        snapshot = await queue_snapshot(download_service())
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "connections": connection_manager().snapshot(),
                # Tallied by tab rather than by raw status, so a metric here and
                # the tab strip on `/candidates` can never show two different
                # numbers for the same queue.
                "candidate_counts": candidate_tab_counts(
                    await database.candidate_counts()
                ),
                "attention": snapshot["attention"],
            },
        )

    #: The six tab routes. Declared above `/candidates/{candidate_id}` because
    #: Starlette matches in declaration order and that route types its parameter
    #: as `int`: below it, `/candidates/approved` would be answered by the detail
    #: page and refused as an unparsable id.
    @app.get("/candidates")
    async def candidate_queue(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidates(request, "pending", error=error)

    @app.get("/candidates/all")
    async def all_candidates(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidates(request, "all", error=error)

    @app.get("/candidates/needs-info")
    async def needs_info_queue(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidates(request, "needs_info", error=error)

    @app.get("/candidates/approved")
    async def approved_queue(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidates(request, "approved", error=error)

    @app.get("/candidates/rejected")
    async def rejected_queue(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidates(request, "rejected", error=error)

    @app.get("/candidates/failed")
    async def failed_queue(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidates(request, "failed", error=error)

    #: 处理中 was its own page before R5 and is now part of 已通过, which covers
    #: APPROVED, PROCESSING and DOWNLOADED. Kept as a redirect rather than
    #: deleted, for the reason `/downloads` was: a bookmark that used to work
    #: should not 404. 307 so no browser caches it and makes the path
    #: unreclaimable.
    @app.get("/candidates/processing")
    async def processing_queue(request: Request):
        return RedirectResponse("/candidates/approved", status_code=307)

    async def _render_work(
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
            database,
            candidate_id,
            download=download_service(),
            sources=configured_sources(request),
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        return templates.TemplateResponse(
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

    @app.get("/works/{candidate_id}")
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
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_work(
            request, candidate_id, error=error, message=message
        )

    @app.get("/manual-add")
    async def manual_add_page(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="manual_add.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "exhentai_configured": await _exhentai_configured(),
                "torrent_configured": await _torrent_configured(),
                "error": None,
                "success": None,
            },
        )

    @app.post("/manual-add")
    async def manual_add_submit(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        form = await request.form()
        validate_csrf(request, str(form.get("csrf_token") or ""))
        raw = str(form.get("input") or "").strip()
        try:
            candidate_id = await _ingest_manual_link(raw)
        except (ReviewError, ExHentaiDownloadError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="manual_add.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "exhentai_configured": await _exhentai_configured(),
                    "torrent_configured": await _torrent_configured(),
                    "error": str(getattr(exc, "public_message", exc)),
                    "success": None,
                },
                status_code=400,
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    async def _candidates_redirect(
        request: Request, error: str | None = None
    ) -> RedirectResponse:
        """Back to the tab the operator submitted from, error and all.

        The tab travels in a hidden form field rather than being read off the
        referer: a batch approved from 待补充 must not drop the operator onto
        待审核, and unlike the activity page the choice is one of six, which is
        more than a header sniff should be deciding.
        """
        target = str((await request.form()).get("tab") or "")
        entry = next(
            (item for item in CANDIDATE_PAGE_TABS if item["key"] == target),
            CANDIDATE_PAGE_TABS[1],
        )
        href = entry["href"]
        if error:
            href = f"{href}?error={quote_plus(error)}"
        return RedirectResponse(href, status_code=303)

    @app.post("/candidates/batch-review")
    async def batch_review(request: Request):
        """The bulk toolbar, without JavaScript.

        Runs through `apply_review_batch`, the same coroutine
        `POST /api/v1/candidates/batch` uses, so the form and the API cannot
        disagree about what a batch does or about which candidates it skips.
        Skips are folded into the redirect's message: a form post has nowhere
        else to report that three of eight were already approved.
        """
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        form = await request.form()
        validate_csrf(request, str(form.get("csrf_token") or ""))
        action = str(form.get("action") or "")
        try:
            candidate_ids = list(
                dict.fromkeys(
                    int(value) for value in form.getlist("candidate_ids")
                )
            )
        except ValueError:
            candidate_ids = []
        if action not in REVIEW_BATCH_ACTIONS:
            return await _candidates_redirect(
                request, f"未知的审核动作：{action}"
            )
        if not candidate_ids:
            return await _candidates_redirect(request, "请至少选择一条候选")
        operator = request.session.get("username", "admin")
        try:
            result = await apply_review_batch(
                review_orchestrator,
                action,
                candidate_ids,
                operator,
                announce_candidate=lambda candidate_id: (
                    app.state.event_bus.publish(
                        EVENT_CANDIDATE, candidate_id=candidate_id
                    )
                ),
                announce_job=lambda job_id: app.state.event_bus.publish(
                    EVENT_DOWNLOAD, job_id=job_id
                ),
            )
        except ApiError as exc:
            return await _candidates_redirect(request, exc.message)
        skipped = result["skipped"]
        if not skipped:
            return await _candidates_redirect(request)
        return await _candidates_redirect(
            request,
            f"{len(result['applied'])} 条已处理，{len(skipped)} 条跳过："
            f"{skipped[0]['message']}",
        )

    @app.get("/candidates/{candidate_id}")
    async def candidate_detail(request: Request, candidate_id: int):
        """The retired detail path, kept as a redirect to `/works/{id}`.

        Until R6 this rendered the page and `/works/{id}` bounced here; the two
        have swapped. 307 rather than 301 for the same reason as every other
        retirement in this refactor — a permanent redirect cached in a browser
        makes the path unreclaimable. The route keeps its name so that
        `url_for('candidate_detail', ...)` in anything still holding the old
        reference resolves instead of raising.
        """
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=307,
        )

    @app.post("/candidates/{candidate_id}/approve")
    async def approve_candidate(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
        tab: str | None = Form(None),
    ):
        """Approve one candidate and enqueue its download.

        Both the detail page and the list's 行内快速通过 post here, so there is
        one approve path rather than a shortcut that could drift from it. `tab`
        is only sent by the list, and its presence is what says「回到列表」: a
        quick approve must not teleport the operator into a detail page they did
        not ask to open.
        """
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        operator = request.session.get("username", "admin")
        try:
            await _approve_candidates_and_enqueue([candidate_id], operator)
        except ReviewError as exc:
            if tab is not None:
                return await _candidates_redirect(request, exc.public_message)
            return await _render_review_error(
                request,
                candidate_id,
                exc.public_message,
            )
        if tab is not None:
            return await _candidates_redirect(request)
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/reject")
    async def reject_candidate(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        operator = request.session.get("username", "admin")
        try:
            await review_service().reject_candidate(candidate_id, operator)
        except ReviewError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/needs-revision")
    async def needs_revision_candidate(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
        reason: str = Form(""),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        operator = request.session.get("username", "admin")
        try:
            await review_service().request_revision(
                candidate_id, operator, reason
            )
        except ReviewError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/requeue")
    async def requeue_candidate(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        operator = request.session.get("username", "admin")
        form = await request.form()
        note = str(form.get("note") or "").strip() or None
        try:
            await review_service().requeue_candidate(candidate_id, operator, note)
        except ReviewError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/metadata")
    async def edit_metadata(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
        field_name: str = Form(),
        field_value: str = Form(""),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        operator = request.session.get("username", "admin")
        try:
            await review_service().set_manual_metadata(
                candidate_id, operator, field_name, field_value
            )
        except ReviewError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/download")
    async def download_candidate(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        candidate = await database.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        archive_attachments = []
        for message in candidate.messages:
            for attachment in message.attachments:
                if attachment.get("type") == "archive":
                    archive_attachments.append(attachment)
        if not archive_attachments:
            return await _render_review_error(
                request,
                candidate_id,
                "该候选没有可下载的压缩附件",
            )
        try:
            await download_service().enqueue_telegram_download(
                candidate_id, archive_attachments[0]
            )
        except DownloadError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    #: The three tabs of the activity domain, with the label each one wears as a
    #: heading and as a tab. Defined once so the tab strip, the `<title>` and the
    #: page heading cannot disagree, and so adding a tab is one entry.
    ACTIVITY_TABS: tuple[tuple[str, str, str, str], ...] = (
        (
            "queue",
            "/activity",
            "队列",
            "Telegram 附件、EH 种子、Archive Download 与预览页四条来源共用一个下载队列",
        ),
        (
            "packing",
            "/activity/packing",
            "打包",
            "下载完成后的解压与 CBZ 打包任务。它们不占用下载并发，所以单独一个队列",
        ),
        (
            "history",
            "/activity/history",
            "历史",
            "所有已结束的任务：完成、失败与取消。终态记录不会被清理",
        ),
    )

    async def _render_activity(
        request: Request,
        tab: str,
        error: str | None = None,
    ):
        """Render one activity tab.

        All three tabs read the same snapshot, even 历史: the tab counts and the
        needs-attention banner are shown on every tab, because a packaging job
        stuck on a password is not something the operator should have to change
        tab to discover.
        """
        snapshot = await queue_snapshot(download_service())
        counts = {
            "queue": snapshot["counts"]["downloads"],
            "packing": snapshot["counts"]["packing"],
            "history": None,
        }
        current = next(entry for entry in ACTIVITY_TABS if entry[0] == tab)
        context = {
            "csrf_token": request.session["csrf_token"],
            "tab": tab,
            "tab_title": current[2],
            "tab_description": current[3],
            "tabs": [
                {
                    "key": key,
                    "href": href,
                    "label": label,
                    "count": counts[key],
                }
                for key, href, label, _ in ACTIVITY_TABS
            ],
            "snapshot": snapshot,
            "error": error,
            "queue_columns": [
                {"key": "select", "label": "选择"},
                {"key": "job", "label": "任务"},
                {"key": "candidate", "label": "候选"},
                {"key": "provider", "label": "来源"},
                {"key": "priority", "label": "优先级", "numeric": True},
                {"key": "attempt", "label": "尝试", "numeric": True},
                {"key": "artifact", "label": "产出"},
                {"key": "actions", "label": "操作"},
            ],
            # History has neither a checkbox nor an action column: every row is
            # terminal, so there is nothing to select it for.
            "history_columns": [
                {"key": "job", "label": "任务"},
                {"key": "candidate", "label": "候选"},
                {"key": "provider", "label": "来源"},
                {"key": "priority", "label": "优先级", "numeric": True},
                {"key": "attempt", "label": "尝试", "numeric": True},
                {"key": "artifact", "label": "产出"},
            ],
            "default_priority": DEFAULT_JOB_PRIORITY,
            "min_priority": MIN_JOB_PRIORITY,
            "max_priority": MAX_JOB_PRIORITY,
            "empty_title": (
                "暂无打包任务" if tab == "packing" else "暂无进行中的下载任务"
            ),
            "empty_hint": (
                "下载完成的任务会自动进入打包队列"
                if tab == "packing"
                else "在已审核候选详情页触发下载后会出现在这里"
            ),
        }
        if tab == "history":
            context["history_jobs"] = [
                job_summary(job)
                for job in await download_service().list_history_jobs()
            ]
        return templates.TemplateResponse(
            request=request, name="activity.html", context=context
        )

    @app.get("/activity")
    async def activity_queue(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_activity(request, "queue", error)

    @app.get("/activity/packing")
    async def activity_packing(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_activity(request, "packing", error)

    @app.get("/activity/history")
    async def activity_history(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_activity(request, "history")

    #: The pre-R4 paths. Kept as redirects rather than deleted: an operator's
    #: bookmark and any link in an old Telegram notification both point here, and
    #: a 404 on a page that used to work is worse than one extra hop. 307 rather
    #: than 301 so a browser does not cache the redirect forever, which would
    #: make the paths impossible to reclaim.
    @app.get("/downloads")
    async def downloads_dashboard(request: Request):
        return RedirectResponse("/activity", status_code=307)

    @app.get("/downloads/history")
    async def downloads_history(request: Request):
        return RedirectResponse("/activity/history", status_code=307)

    async def _activity_redirect(
        request: Request, error: str | None = None
    ) -> RedirectResponse:
        """Back to the tab the operator submitted from, error and all.

        The referer decides, so acting on a packaging job from 打包 does not drop
        the operator onto 队列. It is only ever used to pick between two known
        paths -- never followed -- so a forged header buys nothing.
        """
        referer = request.headers.get("referer") or ""
        target = (
            "/activity/packing" if "/activity/packing" in referer else "/activity"
        )
        if error:
            target = f"{target}?error={quote_plus(error)}"
        return RedirectResponse(target, status_code=303)

    @app.post("/activity/jobs/batch")
    async def activity_batch(
        request: Request,
        csrf_token: str = Form(),
        action: str = Form(),
        job_ids: list[int] = Form(default=[]),
        priority: int | None = Form(default=None),
        provider: str | None = Form(default=None),
    ):
        """The bulk toolbar, without JavaScript.

        Runs through `apply_job_batch`, the same coroutine
        `POST /api/v1/jobs/batch` uses, so the form and the API cannot disagree
        about what a batch does or about which jobs it skips. Skips are folded
        into the redirect's error text: a form post has nowhere else to report
        that three of eight jobs were already cancelled.
        """
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        if action not in BATCH_JOB_ACTIONS:
            return await _activity_redirect(request, f"未知的任务动作：{action}")
        if not job_ids:
            return await _activity_redirect(request, "请至少选择一个任务")
        try:
            result = await apply_job_batch(
                download_service(),
                action,
                list(dict.fromkeys(job_ids)),
                provider=provider,
                priority=priority,
                announce=lambda job_id: app.state.event_bus.publish(
                    EVENT_DOWNLOAD, job_id=job_id
                ),
            )
        except ApiError as exc:
            return await _activity_redirect(request, exc.message)
        skipped = result["skipped"]
        if not skipped:
            return await _activity_redirect(request)
        return await _activity_redirect(
            request,
            f"{len(result['applied'])} 个任务已执行，{len(skipped)} 个跳过："
            f"{skipped[0]['message']}",
        )

    async def _job_action(
        request: Request,
        csrf_token: str,
        action,
        job_id: int,
        return_to: str | None = None,
    ):
        """Run one queue action and return where the form came from, either way.

        `return_to` is how the work detail page keeps the operator on itself
        without a second set of job routes; it is validated by
        `local_return_to`, and anything else falls back to the activity page.
        """
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        target = local_return_to(return_to)
        try:
            await action(job_id)
        except DownloadError as exc:
            if target:
                return RedirectResponse(
                    f"{target}?error={quote_plus(exc.public_message)}",
                    status_code=303,
                )
            return await _activity_redirect(request, exc.public_message)
        app.state.event_bus.publish(EVENT_DOWNLOAD, job_id=job_id)
        if target:
            return RedirectResponse(target, status_code=303)
        return await _activity_redirect(request)

    @app.post("/activity/jobs/{job_id}/switch-source")
    async def switch_download_source(
        request: Request,
        job_id: int,
        csrf_token: str = Form(),
        provider: str = Form(),
        return_to: str | None = Form(None),
    ):
        """Move a stalled torrent to another source at the operator's request.

        A stall is never resolved automatically: dropping to preview grade or
        spending GP are both choices the service refuses to make for someone.

        Declared above the catch-all below, for the same reason the API's copy is:
        Starlette matches routes in declaration order, so `/jobs/5/switch-source`
        would otherwise be answered by `activity_job_action` and refused as an
        unknown action.
        """
        return await _job_action(
            request,
            csrf_token,
            lambda target: download_service().switch_source(target, provider),
            job_id,
            return_to,
        )

    @app.post("/activity/jobs/{job_id}/{action}")
    async def activity_job_action(
        request: Request,
        job_id: int,
        action: str,
        csrf_token: str = Form(),
        return_to: str | None = Form(None),
    ):
        """One row's action button, without JavaScript.

        The action table is `app.api.actions.JOB_ACTIONS`, shared with the JSON
        API, so the form fallback can never offer a verb the API does not have.
        """
        method_name = JOB_ACTIONS.get(action)
        if method_name is None:
            local = local_return_to(return_to)
            if local:
                return RedirectResponse(
                    f"{local}?error={quote_plus(f'未知的任务动作：{action}')}",
                    status_code=303,
                )
            return await _activity_redirect(request, f"未知的任务动作：{action}")
        return await _job_action(
            request,
            csrf_token,
            getattr(download_service(), method_name),
            job_id,
            return_to,
        )

    @app.post("/candidates/{candidate_id}/exhentai-metadata")
    async def fetch_exhentai_metadata(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            await exhentai_service().fetch_metadata_for_candidate(candidate_id)
        except ExHentaiDownloadError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/exhentai-archive")
    async def download_exhentai_archive(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            await exhentai_service().download_archive_for_candidate(candidate_id)
        except ExHentaiDownloadError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/telegraph")
    async def download_telegraph_preview(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        """Fetch the preview page on demand.

        Queued rather than run inline: a 78-page book takes far longer than a
        request should, and the queue already reports progress and failures.
        """
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            telegraph_service()
            await download_service().enqueue_telegraph_download(candidate_id)
        except (DownloadError, TelegraphError) as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/torrent")
    async def download_torrent(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        """Queue the EH torrent route on demand.

        Queued rather than run inline for the same reason as every other
        source, plus one of its own: the transfer is the client's work and may
        take hours, so there is nothing useful to return synchronously.
        """
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            torrent_service()
            await download_service().enqueue_torrent_download(candidate_id)
        except (DownloadError, TorrentError) as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/candidates/{candidate_id}/convert")
    async def convert_candidate(
        request: Request,
        candidate_id: int,
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            await conversion_service().enqueue_for_candidate(candidate_id)
        except ConversionError as exc:
            return await _render_review_error(
                request, candidate_id, exc.public_message
            )
        return RedirectResponse(
            request.url_for("work_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    async def _render_review_error(
        request: Request, candidate_id: int, message: str
    ):
        """A refused action re-renders the detail page with the reason on it.

        This is one call into `_render_work` rather than a second assembly of the
        same context: R5's lesson was that two renderings of one page drift, and
        an operator reading「无法通过」needs the timeline that explains why, not a
        reduced page that only carries the message.
        """
        return await _render_work(
            request, candidate_id, error=message, status_code=400
        )

    @app.get("/sources")
    async def sources_page(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="sources.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "sources": await database.list_telegram_sources(),
            },
        )

    @app.get("/ui-kit")
    async def ui_kit_page(request: Request):
        # Behind the session like every other page: it is a developer tool, not
        # public documentation, and an unauthenticated route here would be one
        # more surface to keep honest for no benefit.
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="ui_kit.html",
            context={"demo": ui_kit_context()},
        )

    @app.post("/sources")
    async def configure_source(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        form = await request.form()
        validate_csrf(request, str(form.get("csrf_token") or ""))
        source_type = str(form.get("source_type") or "")
        display_name = str(form.get("display_name") or "").strip()
        try:
            chat_id = int(str(form.get("chat_id") or ""))
            max_attachment_size_mb = int(
                str(form.get("max_attachment_size_mb") or "0")
            )
        except ValueError:
            chat_id = 0
            max_attachment_size_mb = -1
        valid_identity = (
            source_type == "CHANNEL" and chat_id < 0
        ) or (
            source_type == "PRIVATE_CHAT" and chat_id > 0
        )
        if not valid_identity or not display_name or max_attachment_size_mb < 0:
            return templates.TemplateResponse(
                request=request,
                name="sources.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "sources": await database.list_telegram_sources(),
                    "error": "来源类型、ID、名称或附件上限无效",
                },
                status_code=400,
            )
        submitted_formats = set(form.getlist("allowed_archive_formats"))
        allowed_archive_formats = tuple(
            archive_format
            for archive_format in ("zip", "rar", "7z", "cbz")
            if archive_format in submitted_formats
        )
        required_tags = _parse_csv_tags(
            form.get("required_tags")
        )
        forbidden_tags = _parse_csv_tags(
            form.get("forbidden_tags")
        )
        allowed_languages = _parse_csv_tags(
            form.get("allowed_languages")
        )
        allowed_categories = _parse_csv_tags(
            form.get("allowed_categories")
        )
        min_rating_raw = str(form.get("min_rating") or "").strip()
        min_rating: float | None = None
        if min_rating_raw:
            try:
                min_rating = float(min_rating_raw)
            except ValueError:
                min_rating = -1.0
        if min_rating is not None and min_rating < 0:
            return templates.TemplateResponse(
                request=request,
                name="sources.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "sources": await database.list_telegram_sources(),
                    "error": "\u6700\u4f4e\u8bc4\u5206\u683c\u5f0f\u65e0\u6548",
                },
                status_code=400,
            )
        await database.configure_telegram_source(
            source_type=source_type,
            chat_id=chat_id,
            display_name=display_name,
            enabled=form.get("enabled") == "on",
            allowed_archive_formats=allowed_archive_formats,
            max_attachment_size_mb=max_attachment_size_mb,
            required_tags=required_tags,
            forbidden_tags=forbidden_tags,
            allowed_languages=allowed_languages,
            allowed_categories=allowed_categories,
            min_rating=min_rating,
        )
        return RedirectResponse(request.url_for("sources_page").path, status_code=303)

    async def _archive_settings_context(
        request: Request,
        error: str | None = None,
        notice: str | None = None,
    ) -> dict:
        service = archive_settings_service()
        return {
            "csrf_token": request.session["csrf_token"],
            "profiles": await service.profiles(),
            "limits": await service.limits(),
            "passwords": await service.passwords(),
            "keep_original": await service.keep_original(),
            "image_quality": await service.image_quality_view(),
            "toolchain": await service.toolchain_status(),
            "paths": await service.paths(),
            "torrent": await service.torrent_client_view(),
            "auto_pack_after_download": await service.auto_pack_after_download(),
            "torrent_enabled": app.state.torrent_service is not None,
            "notice": notice,
            "default_paths": {
                "library": str(app_settings.library_path),
                "work": str(app_settings.work_path),
            },
            "error": error,
        }

    async def _render_archive_settings(
        request: Request,
        error: str | None = None,
        status_code: int = 200,
        notice: str | None = None,
    ):
        return templates.TemplateResponse(
            request=request,
            name="archive_settings.html",
            context=await _archive_settings_context(
                request, error, notice
            ),
            status_code=status_code,
        )

    @app.get("/archive-settings")
    async def archive_settings_page(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_archive_settings(request)

    @app.post("/archive-settings/auto-pack")
    async def save_auto_pack_after_download(
        request: Request, csrf_token: str = Form()
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        form = await request.form()
        await archive_settings_service().save_auto_pack_after_download(
            form.get("enabled") == "on"
        )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/paths")
    async def save_archive_paths(request: Request, csrf_token: str = Form()):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        form = await request.form()
        try:
            await archive_settings_service().save_paths(
                {
                    "library_path": str(form.get("library_path") or ""),
                    "work_path": str(form.get("work_path") or ""),
                }
            )
        except ArchiveSettingsError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/torrent")
    async def save_torrent_client_settings(
        request: Request, csrf_token: str = Form()
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        form = await request.form()
        try:
            await archive_settings_service().save_torrent_client(
                {
                    "base_url": form.get("base_url"),
                    "username": form.get("username"),
                    "password": form.get("password"),
                    "category": form.get("category"),
                    "save_path": form.get("save_path"),
                    "local_save_path": form.get("local_save_path"),
                    "keep_seeding": bool(form.get("keep_seeding")),
                    "auto_pack": bool(form.get("auto_pack")),
                }
            )
        except ArchiveSettingsError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/torrent-test")
    async def test_torrent_client(request: Request, csrf_token: str = Form()):
        """Prove the stored settings reach a real client before a book needs it."""
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            version = await torrent_service().check_connection()
        except TorrentError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return await _render_archive_settings(
            request,
            notice=f"qBittorrent \u8fde\u901a\uff0c\u7248\u672c {version}",
        )

    @app.post("/archive-settings/limits")
    async def save_archive_limits(request: Request, csrf_token: str = Form()):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        form = await request.form()
        try:
            # Limits are typed by hand and are the field that realistically
            # fails validation, so they are stored first: a rejected number
            # aborts before the quality level is touched.
            await archive_settings_service().save_limits(
                {key: str(form.get(key) or "") for key in ARCHIVE_LIMIT_KEYS}
            )
            await archive_settings_service().save_keep_original(
                form.get("keep_original") == "on"
            )
            await archive_settings_service().save_image_quality(
                str(form.get("image_quality") or "")
            )
        except ArchiveSettingsError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/profiles/{name}")
    async def save_archive_tool_profile(
        request: Request, name: str, csrf_token: str = Form()
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        form = await request.form()
        timeout_raw = str(form.get("timeout_seconds") or "").strip()
        try:
            timeout_seconds = int(timeout_raw) if timeout_raw else None
        except ValueError:
            return await _render_archive_settings(
                request, "超时时长必须是整数", status_code=400
            )
        executable_raw = str(form.get("executable_path") or "").strip()
        try:
            await archive_settings_service().set_profile_state(
                name,
                enabled=form.get("enabled") == "on",
                executable_path=executable_raw or None,
                timeout_seconds=timeout_seconds,
            )
        except ArchiveSettingsError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/toolchain/install")
    async def install_archive_toolchain(
        request: Request, csrf_token: str = Form()
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            await archive_settings_service().install_toolchain(force=True)
        except ArchiveSettingsError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/passwords")
    async def add_archive_password(request: Request, csrf_token: str = Form()):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        form = await request.form()
        priority_raw = str(form.get("priority") or "100").strip()
        try:
            priority = int(priority_raw)
        except ValueError:
            return await _render_archive_settings(
                request, "优先级必须是整数", status_code=400
            )
        try:
            await archive_settings_service().add_password(
                name=str(form.get("name") or ""),
                password=str(form.get("password") or ""),
                priority=priority,
                enabled=form.get("enabled") == "on",
            )
        except ArchiveSettingsError as exc:
            return await _render_archive_settings(
                request, exc.public_message, status_code=400
            )
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.post("/archive-settings/passwords/{password_id}/delete")
    async def delete_archive_password(
        request: Request, password_id: int, csrf_token: str = Form()
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        await archive_settings_service().delete_password(password_id)
        return RedirectResponse(
            request.url_for("archive_settings_page").path, status_code=303
        )

    @app.get("/connections")
    async def connections_page(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="connections.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "connections": connection_manager().snapshot(),
            },
        )

    @app.post("/connections/telegram")
    async def configure_telegram(
        request: Request,
        bot_token: str = Form(),
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            await connection_manager().configure_telegram(bot_token)
        except ProviderConnectionError:
            return templates.TemplateResponse(
                request=request,
                name="connections.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "connections": connection_manager().snapshot(),
                },
                status_code=400,
            )
        return RedirectResponse(
            request.url_for("connections_page").path, status_code=303
        )

    @app.post("/connections/telegram/disconnect")
    async def disconnect_telegram(request: Request, csrf_token: str = Form()):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        await connection_manager().disconnect_telegram()
        return RedirectResponse(
            request.url_for("connections_page").path, status_code=303
        )

    @app.post("/connections/exhentai")
    async def configure_exhentai(
        request: Request,
        ipb_member_id: str = Form(),
        ipb_pass_hash: str = Form(),
        igneous: str = Form(),
        csrf_token: str = Form(),
    ):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        try:
            await connection_manager().configure_exhentai(
                ExHentaiCredentials(ipb_member_id, ipb_pass_hash, igneous)
            )
        except ProviderConnectionError:
            return templates.TemplateResponse(
                request=request,
                name="connections.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "connections": connection_manager().snapshot(),
                },
                status_code=400,
            )
        return RedirectResponse(
            request.url_for("connections_page").path, status_code=303
        )

    @app.post("/connections/exhentai/disconnect")
    async def disconnect_exhentai(request: Request, csrf_token: str = Form()):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        await connection_manager().disconnect_exhentai()
        return RedirectResponse(
            request.url_for("connections_page").path, status_code=303
        )

    @app.get("/api/connections/status")
    async def connection_status(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return asdict(connection_manager().snapshot())

    @app.get("/login")
    async def login_page(request: Request):
        csrf_token = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf_token": csrf_token},
        )

    @app.post("/login")
    async def login(
        request: Request,
        password: str = Form(),
        csrf_token: str = Form(),
    ):
        validate_csrf(request, csrf_token)
        client_key = request.client.host if request.client else "unknown"
        failed_count, locked_until = login_attempts.get(client_key, (0, 0.0))
        now = time.monotonic()
        if locked_until > now:
            raise HTTPException(status_code=429, detail="Too many login attempts")
        if locked_until:
            failed_count = 0
            login_attempts.pop(client_key, None)
        admin_auth = await database.get_admin_auth("admin")
        if admin_auth is None:
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        try:
            password_matches = await asyncio.to_thread(
                password_hasher.verify, password, admin_auth[0]
            )
        except PwdlibError as exc:
            raise HTTPException(
                status_code=503, detail="Authentication is not configured"
            ) from exc
        if not password_matches:
            failed_count += 1
            login_attempts[client_key] = (
                failed_count,
                now + 60 if failed_count >= 5 else 0.0,
            )
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "error": "密码不正确",
                },
                status_code=401,
            )
        login_attempts.pop(client_key, None)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["username"] = "admin"
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        request.session["must_change_password"] = not admin_auth[1]
        destination = (
            request.url_for("change_password_page").path
            if not admin_auth[1]
            else request.url_for("dashboard").path
        )
        return RedirectResponse(destination, status_code=303)

    @app.get("/change-password")
    async def change_password_page(request: Request):
        if not request.session.get("authenticated"):
            return RedirectResponse(
                request.url_for("login_page").path, status_code=303
            )
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={"csrf_token": request.session["csrf_token"]},
        )

    @app.post("/change-password")
    async def change_password(
        request: Request,
        current_password: str = Form(),
        new_password: str = Form(),
        confirmation: str = Form(),
        csrf_token: str = Form(),
    ):
        if not request.session.get("authenticated"):
            return RedirectResponse(
                request.url_for("login_page").path, status_code=303
            )
        validate_csrf(request, csrf_token)
        error: str | None = None
        if len(new_password) < 12:
            error = "新密码至少需要 12 个字符"
        elif new_password != confirmation:
            error = "两次输入的新密码不一致"
        admin_auth = await database.get_admin_auth("admin")
        if admin_auth is None:
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        try:
            current_password_matches = await asyncio.to_thread(
                password_hasher.verify, current_password, admin_auth[0]
            )
        except PwdlibError as exc:
            raise HTTPException(
                status_code=503, detail="Authentication is not configured"
            ) from exc
        if not current_password_matches:
            error = "当前密码不正确"
        if error:
            return templates.TemplateResponse(
                request=request,
                name="change_password.html",
                context={
                    "csrf_token": request.session["csrf_token"],
                    "error": error,
                },
                status_code=400,
            )
        new_password_hash = await asyncio.to_thread(
            password_hasher.hash, new_password
        )
        await database.change_admin_password("admin", new_password_hash)
        await asyncio.to_thread(remove_bootstrap_password, app_settings.data_path)
        request.session["must_change_password"] = False
        return RedirectResponse(request.url_for("dashboard").path, status_code=303)

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form()):
        validate_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse(request.url_for("login_page").path, status_code=303)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        errors = list(app.state.startup_errors)
        if not await database.check_writable():
            errors.append("database is not writable")
        for name, path in (
            ("data", app_settings.data_path),
            ("library", app_settings.library_path),
            ("work", app_settings.work_path),
        ):
            try:
                await asyncio.to_thread(ensure_writable_directory, path)
            except OSError:
                errors.append(f"{name} directory is not writable")
        if errors:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "errors": errors},
            )
        return JSONResponse(content={"status": "ready"})

    return app


app = create_app()

