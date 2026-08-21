import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import quote_plus
import hmac
import json
import logging
from pathlib import Path
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
from app.errors import AppError, app_error_handler
from app.logging import configure_logging

from app.review.models import (
    METADATA_FIELDS,
    REVIEWABLE_STATUSES,
    field_label,
    split_metadata_entries,
)
from app.review.service import ReviewError, ReviewService
from app.downloads.models import (
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
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
from app.telegraph.models import TelegraphError
from app.telegraph.service import TelegraphService
from app.torrent.models import TorrentError
from app.torrent.service import TorrentService
from app.exhentai.tagdb_sync import TagDatabaseError, TagDatabaseSync
from app.secrets import SecretStore
from app.storage.readiness import ensure_writable_directory



#: Telegram Bot API `getFile` refuses anything larger, permanently. Routing
#: past an oversized attachment is what makes the rest of the chain useful.
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024


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

def create_app(
    settings: Settings | None = None,
    *,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
    exhentai_transport: httpx.AsyncBaseTransport | None = None,
    tagdb_transport: httpx.AsyncBaseTransport | None = None,
    telegraph_transport: httpx.AsyncBaseTransport | None = None,
    telegraph_resolver=None,
    torrent_client_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    configure_logging()
    app_settings = settings or Settings.from_env()
    database = Database(app_settings.data_path / "ehbot.db")
    password_hasher = PasswordHash.recommended()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        app.state.startup_errors = app_settings.readiness_errors()
        connection_manager: ConnectionManager | None = None
        telegram_client: httpx.AsyncClient | None = None
        exhentai_client: httpx.AsyncClient | None = None
        tagdb_client: httpx.AsyncClient | None = None
        telegraph_client: httpx.AsyncClient | None = None
        torrent_client: httpx.AsyncClient | None = None
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
                work_path_provider=archive_settings_service.work_path,
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
    app.state.tag_translator = None
    app.add_exception_handler(AppError, app_error_handler)
    login_attempts: dict[str, tuple[int, float]] = {}
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.app_secret_key or secrets.token_urlsafe(32),
        https_only=app_settings.session_cookie_secure,
        same_site="lax",
    )
    templates = Jinja2Templates(
        directory=Path(__file__).parent / "web" / "templates"
    )

    def _status_label(status: str) -> str:
        labels = {
            "CONVERSION_PENDING": "待转换",
            "CONVERSION_RUNNING": "转换中",
            "CONVERSION_COMPLETED": "已转换",
            "CONVERSION_FAILED": "转换失败",
            "CONVERSION_WAITING_VOLUMES": "待补分卷",
            "CONVERSION_WAITING_PASSWORD": "待补密码",
            "PENDING_REVIEW": "待审核",
            "NEEDS_INFO": "待补充",
            "APPROVED": "已通过",
            "REJECTED": "已驳回",
            "NEEDS_REVISION": "需要修订",
            "PROCESSING": "处理中",
            "FAILED": "失败",
            "DOWNLOADED": "已下载",
        }
        return labels.get(status, status)

    templates.env.filters["status_label"] = _status_label
    templates.env.globals["status_label"] = _status_label
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

    def review_service() -> ReviewService:
        return ReviewService(database)

    def _route_download_source(candidate) -> tuple[str | None, dict | None]:
        """Pick the best available source for a candidate.

        The order is quality first, cost second:

        1. `TELEGRAM` — the uploader's own archive, original quality and free,
           but Bot API `getFile` caps a single file at 20 MB.
        2. `EH_TORRENT` — original quality and free, routed whenever gdata
           reported a torrent and a client is configured. This is the route
           an oversized book normally takes.
        3. `TELEGRAPH` — the preview page, complete but re-encoded to 1280 px,
           so roughly 5–10 % of the original bytes. Last resort.

        ExHentai Archive Download is deliberately absent: it spends GP, and
        spending a limited resource is an operator decision rather than a
        routing default. The review page offers it as a button.
        """
        attachment = next(
            (
                item
                for message in candidate.messages
                for item in message.attachments
                if item.get("type") == "archive"
                and int(item.get("size_bytes") or 0) <= TELEGRAM_FILE_LIMIT
            ),
            None,
        )
        if attachment is not None:
            return PROVIDER_TELEGRAM, attachment
        if (
            candidate.torrent_hash
            and app.state.torrent_service is not None
        ):
            return PROVIDER_EH_TORRENT, None
        if (
            candidate.preview_url
            and app.state.telegraph_service is not None
        ):
            return PROVIDER_TELEGRAPH, None
        return None, None

    async def _approve_candidates_and_enqueue(
        candidate_ids: list[int], operator: str
    ) -> tuple[int, ...]:
        targets: list[tuple[int, str, dict | None]] = []
        for candidate_id in candidate_ids:
            candidate = await database.get_candidate(candidate_id)
            if candidate is None:
                raise ReviewError(
                    "CANDIDATE_NOT_FOUND", "候选不存在或已被删除"
                )
            if candidate.status not in REVIEWABLE_STATUSES:
                raise ReviewError(
                    "REVIEW_INVALID_TRANSITION",
                    f"候选 #{candidate_id} 当前状态不可审核",
                )
            provider, attachment = _route_download_source(candidate)
            if provider is None:
                raise ReviewError(
                    "CANDIDATE_NOT_DOWNLOADABLE",
                    f"候选 #{candidate_id} 没有可用的下载来源",
                )
            targets.append((candidate_id, provider, attachment))

        job_ids: list[int] = []
        for candidate_id, provider, attachment in targets:
            await review_service().approve_candidate(candidate_id, operator)
            try:
                if provider == PROVIDER_TELEGRAM:
                    result = await download_service().enqueue_telegram_download(
                        candidate_id, attachment or {}
                    )
                elif provider == PROVIDER_EH_TORRENT:
                    result = await download_service().enqueue_torrent_download(
                        candidate_id
                    )
                elif provider == PROVIDER_TELEGRAPH:
                    result = await download_service().enqueue_telegraph_download(
                        candidate_id
                    )
                else:
                    result = await download_service().enqueue_exhentai_download(
                        candidate_id
                    )
                job_ids.append(result.job_id)
            except DownloadError as exc:
                raise ReviewError(exc.code, exc.public_message) from exc
        return tuple(job_ids)

    async def _apply_automatic_approval(candidate_id: int) -> bool:
        match = await AutomaticApprovalService(database).matching_rule(candidate_id)
        if match is None:
            return False
        try:
            job_ids = await _approve_candidates_and_enqueue(
                [candidate_id], "自动审批"
            )
        except ReviewError:
            return False
        await database.record_review_action(
            candidate_id,
            "AUTO_APPROVE",
            "自动审批",
            {
                "rule_id": match.rule.rule_id,
                "rule_name": match.rule.name,
                "rule_version": match.rule.version,
                "dsl_snapshot": match.rule.dsl_snapshot,
                "condition": match.rule.condition,
                "conditions": match.conditions,
                "metadata": match.metadata,
                "download_job_ids": list(job_ids),
            },
        )
        return True

    async def _reject_candidates(
        candidate_ids: list[int], operator: str
    ) -> None:
        for candidate_id in candidate_ids:
            candidate = await database.get_candidate(candidate_id)
            if candidate is None:
                raise ReviewError(
                    "CANDIDATE_NOT_FOUND", "候选不存在或已被删除"
                )
            if candidate.status not in REVIEWABLE_STATUSES:
                raise ReviewError(
                    "REVIEW_INVALID_TRANSITION",
                    f"候选 #{candidate_id} 当前状态不可审核",
                )
        for candidate_id in candidate_ids:
            await review_service().reject_candidate(candidate_id, operator)

    async def _render_candidate_queue(
        request: Request,
        *,
        status: str,
        queue_title: str,
        queue_description: str,
        empty_title: str,
        empty_text: str,
        batch_enabled: bool = False,
        error: str | None = None,
        status_code: int = 200,
    ):
        candidates = await database.list_candidates(status=status)
        if batch_enabled:
            await exhentai_service().enrich_candidates_for_review(candidates)
            candidates = await database.list_candidates(status=status)
            for candidate in candidates:
                await _apply_automatic_approval(candidate.candidate_id)
            candidates = await database.list_candidates(status=status)
        return templates.TemplateResponse(
            request=request,
            name="candidates.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "candidates": candidates,
                "queue_title": queue_title,
                "queue_description": queue_description,
                "empty_title": empty_title,
                "empty_text": empty_text,
                "batch_enabled": batch_enabled,
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
            ast = validate_rule_ast(json.loads(str(form.get("ast_json") or "")))
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
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "connections": connection_manager().snapshot(),
                "candidate_counts": await database.candidate_counts(),
            },
        )

    @app.get("/candidates")
    async def candidate_queue(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidate_queue(
            request,
            status="PENDING_REVIEW",
            queue_title="待审核队列",
            queue_description="确认元数据后可批量加入下载队列",
            empty_title="暂无待审核候选",
            empty_text="白名单来源的新候选会显示在这里",
            batch_enabled=True,
        )

    @app.get("/candidates/needs-info")
    async def needs_info_queue(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidate_queue(
            request,
            status="NEEDS_INFO",
            queue_title="待补充队列",
            queue_description="需要补全标题或附件信息的漫画候选",
            empty_title="暂无待补充候选",
            empty_text="信息不足的候选会显示在这里",
        )

    @app.get("/candidates/processing")
    async def processing_queue(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidate_queue(
            request,
            status="PROCESSING",
            queue_title="处理中队列",
            queue_description="已审核并正在下载的候选",
            empty_title="暂无处理中候选",
            empty_text="下载 Worker 领取任务后会显示在这里",
        )

    @app.get("/candidates/failed")
    async def failed_queue(request: Request):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        return await _render_candidate_queue(
            request,
            status="FAILED",
            queue_title="失败队列",
            queue_description="下载失败、需要检查后重新处理的候选",
            empty_title="暂无失败候选",
            empty_text="下载失败的候选会显示在这里",
        )

    @app.post("/candidates/batch-review")
    async def batch_review(request: Request):
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
        if not candidate_ids or action not in {"approve", "reject"}:
            return await _render_candidate_queue(
                request,
                status="PENDING_REVIEW",
                queue_title="待审核队列",
                queue_description="确认元数据后可批量加入下载队列",
                empty_title="暂无待审核候选",
                empty_text="白名单来源的新候选会显示在这里",
                batch_enabled=True,
                error="请选择至少一条候选并指定审核操作",
                status_code=400,
            )
        operator = request.session.get("username", "admin")
        try:
            if action == "approve":
                await _approve_candidates_and_enqueue(candidate_ids, operator)
            else:
                await _reject_candidates(candidate_ids, operator)
        except ReviewError as exc:
            return await _render_candidate_queue(
                request,
                status="PENDING_REVIEW",
                queue_title="待审核队列",
                queue_description="确认元数据后可批量加入下载队列",
                empty_title="暂无待审核候选",
                empty_text="白名单来源的新候选会显示在这里",
                batch_enabled=True,
                error=exc.public_message,
                status_code=400,
            )
        return RedirectResponse(
            request.url_for("candidate_queue").path, status_code=303
        )

    @app.get("/candidates/{candidate_id}")
    async def candidate_detail(request: Request, candidate_id: int):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        candidate = await database.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        summary = await review_service().get_candidate_review_summary(candidate_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        jobs = await download_service().list_jobs_for_candidate(candidate_id)
        # Any provider that finished leaves an ARCHIVE artifact, and that is
        # all conversion needs. Testing for TELEGRAM specifically used to hide
        # the button for every other source.
        archive_ready = any(
            job.state == "COMPLETED" and job.artifact_path
            for job in jobs
        )
        return templates.TemplateResponse(
            request=request,
            name="candidate_detail.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "candidate": candidate,
                "metadata_entries": split_metadata_entries(
                    summary.metadata
                )[0],
                "raw_metadata_entries": split_metadata_entries(
                    summary.metadata
                )[1],
                "review_history": summary.review_history,
                "download_jobs": jobs,
                "archive_ready": archive_ready,
                "preview_available": bool(
                    candidate.preview_url
                ) and app.state.telegraph_service is not None,
                # The hash is what makes the route runnable; a count alone
                # would offer a button that can only fail.
                "torrent_available": bool(
                    candidate.torrent_hash
                ) and app.state.torrent_service is not None,
                "torrent_enabled": app.state.torrent_service is not None,
                "metadata_fields": METADATA_FIELDS,
                "field_label": field_label,
                "current_user": request.session.get("username", "admin"),
            },
        )

    @app.post("/candidates/{candidate_id}/approve")
    async def approve_candidate(
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
            await _approve_candidates_and_enqueue([candidate_id], operator)
        except ReviewError as exc:
            return await _render_review_error(
                request,
                candidate_id,
                exc.public_message,
            )
        return RedirectResponse(
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.get("/downloads")
    async def downloads_dashboard(request: Request, error: str | None = None):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        active = await download_service().list_active_jobs()
        # Torrent progress is written by a background poller, so the page has to
        # come back for it. The refresh is armed only while something is
        # actually moving, so an idle dashboard does not reload forever.
        live = any(
            job.is_waiting_for_peers or job.is_seeding for job in active
        )
        return templates.TemplateResponse(
            request=request,
            name="downloads.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "active_jobs": active,
                "error": error,
                "live_refresh_seconds": (
                    app_settings.torrent_poll_seconds if live else 0
                ),
            },
        )

    async def _download_action(
        request: Request, csrf_token: str, action, job_id: int
    ):
        """Run one queue action and return to the dashboard either way."""
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        validate_csrf(request, csrf_token)
        target_url = request.url_for("downloads_dashboard").path
        try:
            await action(job_id)
        except DownloadError as exc:
            return RedirectResponse(
                f"{target_url}?error={quote_plus(exc.public_message)}",
                status_code=303,
            )
        return RedirectResponse(target_url, status_code=303)

    @app.post("/downloads/{job_id}/retry")
    async def retry_download(
        request: Request, job_id: int, csrf_token: str = Form()
    ):
        return await _download_action(
            request, csrf_token, download_service().retry_job, job_id
        )

    @app.post("/downloads/{job_id}/pause")
    async def pause_download(
        request: Request, job_id: int, csrf_token: str = Form()
    ):
        return await _download_action(
            request, csrf_token, download_service().pause_job, job_id
        )

    @app.post("/downloads/{job_id}/resume")
    async def resume_download(
        request: Request, job_id: int, csrf_token: str = Form()
    ):
        return await _download_action(
            request, csrf_token, download_service().resume_job, job_id
        )

    @app.post("/downloads/{job_id}/cancel")
    async def cancel_download(
        request: Request, job_id: int, csrf_token: str = Form()
    ):
        return await _download_action(
            request, csrf_token, download_service().cancel_job, job_id
        )

    @app.post("/downloads/{job_id}/stop-seeding")
    async def stop_seeding(
        request: Request, job_id: int, csrf_token: str = Form()
    ):
        """Stop sharing a finished torrent, at the operator's choice.

        Seeding continues by default because the payload came from the swarm,
        so ending it is an explicit action rather than a side effect of the
        download finishing.
        """
        return await _download_action(
            request, csrf_token, download_service().stop_seeding, job_id
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    @app.post("/downloads/{job_id}/switch-source")
    async def switch_download_source(
        request: Request,
        job_id: int,
        csrf_token: str = Form(),
        provider: str = Form(),
    ):
        """Move a stalled torrent to another source at the operator's request.

        A stall is never resolved automatically: dropping to preview grade or
        spending GP are both choices the service refuses to make for someone.
        """
        return await _download_action(
            request,
            csrf_token,
            lambda target: download_service().switch_source(
                target, provider
            ),
            job_id,
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
            request.url_for("candidate_detail", candidate_id=candidate_id).path,
            status_code=303,
        )

    async def _render_review_error(
        request: Request, candidate_id: int, message: str
    ):
        candidate = await database.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        summary = await review_service().get_candidate_review_summary(candidate_id)
        return templates.TemplateResponse(
            request=request,
            name="candidate_detail.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "candidate": candidate,
                "metadata_entries": split_metadata_entries(
                    summary.metadata if summary is not None else ()
                )[0],
                "raw_metadata_entries": split_metadata_entries(
                    summary.metadata if summary is not None else ()
                )[1],
                "review_history": (
                    summary.review_history if summary is not None else ()
                ),
                "metadata_fields": METADATA_FIELDS,
                "field_label": field_label,
                "current_user": request.session.get("username", "admin"),
                "error": message,
            },
            status_code=400,
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
            "toolchain": await service.toolchain_status(),
            "paths": await service.paths(),
            "torrent": await service.torrent_client_view(),
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
            await archive_settings_service().save_limits(
                {key: str(form.get(key) or "") for key in ARCHIVE_LIMIT_KEYS}
            )
            await archive_settings_service().save_keep_original(
                form.get("keep_original") == "on"
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

