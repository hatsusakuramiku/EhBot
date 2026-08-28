"""Service construction and the application lifespan.

Everything that has to exist before a request can be served, and nothing that
serves one. Until R9 this was ~330 lines nested inside `create_app`, which is
what made that function 2760 lines long and what made the startup order hard to
read: the wiring, the helper closures and 75 routes were interleaved.

Two functions, in the order they are called:

* `seed_state` puts a slot on `app.state` for every service, most of them None.
  Declaring them up front is what lets a route answer 503「服务不可用」instead of
  raising `AttributeError` when startup failed or a source is switched off, and it
  is why `getattr(..., None)` in the deps modules is a check rather than a guess.
* `build_lifespan` returns the async context manager FastAPI runs around the
  server: it opens the database, constructs each service in dependency order,
  starts the workers, and on the way out stops them and closes every HTTP client
  it opened.

Startup is deliberately forgiving. An unwritable library directory, a corrupt
database, a missing 7-Zip: each is appended to `app.state.startup_errors` and the
process still comes up, because a deployment that refuses to boot cannot show the
operator what is wrong with it. `/readyz` answers 503 off that same list, and
since R9 so does the badge on the workbench.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import secrets
import sqlite3

from fastapi import FastAPI, HTTPException
import httpx

from app.api.events import EVENT_CONVERSION, EVENT_DOWNLOAD, EventBus
from app.archive.service import ArchiveSettingsService
from app.bootstrap import (
    format_bootstrap_banner,
    remove_bootstrap_password,
    write_bootstrap_password,
)
from app.candidates.ingestor import CandidateIngestor
from app.connections.exhentai import ExHentaiCredentials
from app.connections.manager import ConnectionManager
from app.conversion.service import ConversionService
from app.downloads.service import DownloadService
from app.exhentai.service import ExHentaiService
from app.exhentai.tagdb import TagTranslator
from app.exhentai.tagdb_sync import TagDatabaseError, TagDatabaseSync
from app.review.orchestration import ReviewOrchestrator
from app.secrets import SecretStore
from app.settings.service import DEFAULT_TIMEZONE, SystemSettingsService
from app.storage.readiness import ensure_writable_directory
from app.telegraph.fetcher import FetchLimits
from app.telegraph.guard import check_image_url
from app.telegraph.service import TelegraphService
from app.thumbnails.service import ThumbnailService
from app.torrent.service import TorrentService

#: 503 details for the two optional sources. Written once because a download
#: callback and a page route both have to be able to say the same thing about the
#: same missing service.
TELEGRAPH_UNAVAILABLE = "Telegraph source is unavailable"
TORRENT_UNAVAILABLE = "Torrent source is unavailable"
TELEGRAM_USER_UNAVAILABLE = "Telegram user account is unavailable"
#: The download service is not optional, but it does not exist until the lifespan
#: has built it, so the orchestrator's lookup has to be able to fail the same way.
DOWNLOADS_UNAVAILABLE = "Downloads are unavailable"


def _required(application: FastAPI, name: str, detail: str):
    """One optional service, or 503.

    Used by the download service callbacks, which are handed to a worker that
    outlives any request: capturing the instance would freeze whatever was
    configured at startup, so a settings change would not take effect until a
    restart.
    """
    service = getattr(application.state, name, None)
    if service is None:
        raise HTTPException(status_code=503, detail=detail)
    return service


async def _telegram_context(secret_store, default_client):
    """The bot token and the client to send it with.

    Read per call rather than captured: the token can be replaced from the 外部连接
    tab while the process runs, and the send path must pick that up without a
    restart.
    """
    token = await asyncio.to_thread(secret_store.read, "telegram_bot_token")
    return token, default_client


async def _exhentai_credentials(secret_store):
    """The stored gallery cookies, or None when they are absent or unreadable.

    None rather than an exception: no credential is a normal state (a fresh
    install), and so is a stored blob this version can no longer parse. Both mean
    the same thing to a caller -- fall back to what works without a session.
    """
    cookies_json = await asyncio.to_thread(secret_store.read, "exhentai_cookies")
    if not cookies_json:
        return None
    try:
        return ExHentaiCredentials.from_json(cookies_json)
    except (ValueError, KeyError):
        return None


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


def seed_state(app: FastAPI, app_settings, database) -> None:
    """Declare every `app.state` slot before the first request can arrive.

    A slot that exists and holds None is the difference between 503「下载服务当前
    不可用」and a 500 from `AttributeError`: startup is allowed to fail, and a
    source is allowed to be switched off, so every reader has to be able to ask.
    """
    app.state.settings = app_settings
    app.state.database = database
    # Constructed eagerly rather than in the lifespan: it holds nothing but the
    # database handle, and `/api/v1/meta` serves the polling cadence from it on a
    # request that can arrive before the workers have started.
    app.state.system_settings_service = SystemSettingsService(
        database,
        default_source_concurrency=app_settings.telegraph_concurrency,
    )
    # Seeded with the default so a page rendered before startup finishes -- or
    # after a startup that failed -- still has a zone to format in. The stored
    # value replaces it once the database is open.
    app.state.display_timezone = DEFAULT_TIMEZONE
    app.state.startup_errors = []
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
    # before any browser has connected (publishing with no subscriber is a no-op,
    # which is what makes it cheap to call from the download loop).
    app.state.event_bus = EventBus()
    # One instance, published, so `app/api` and `app/web` route a download the
    # same way. The availability probes are callables rather than captured
    # booleans because a source can be configured after startup, and the
    # download-service lookup is lazy for the same reason: the orchestrator
    # exists before the lifespan has built one.
    app.state.review_orchestrator = ReviewOrchestrator(
        database,
        lambda: _required(app, "download_service", DOWNLOADS_UNAVAILABLE),
        torrent_available=lambda: app.state.torrent_service is not None,
        telegraph_available=lambda: app.state.telegraph_service is not None,
        # Asked of the connection manager rather than of a setting: whether an
        # oversized attachment can be fetched depends on a session being valid
        # right now, which is the one thing the manager tracks and a boolean
        # captured at startup cannot.
        telegram_user_available=lambda: bool(
            app.state.connection_manager is not None
            and app.state.connection_manager.user_download_available()
        ),
    )


def build_lifespan(
    app_settings,
    database,
    password_hasher,
    session_secret,
    *,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
    telegram_user_client_factory=None,
    exhentai_transport: httpx.AsyncBaseTransport | None = None,
    tagdb_transport: httpx.AsyncBaseTransport | None = None,
    telegraph_transport: httpx.AsyncBaseTransport | None = None,
    telegraph_resolver=None,
    torrent_client_transport: httpx.AsyncBaseTransport | None = None,
    thumbnail_transport: httpx.AsyncBaseTransport | None = None,
    thumbnail_resolver=None,
):
    """Build the startup/shutdown context manager for one application.

    The transports are the test seam: every outbound client is constructed in
    here, so a test injects a stub transport instead of patching the module that
    uses it. That is also why they are keyword-only -- eight positional `None`s at
    a call site say nothing about which host is being faked.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.startup_errors = app_settings.readiness_errors()
        if session_secret.error:
            application.state.startup_errors.append(session_secret.error)
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
            # The shell renders every page's timestamps in this zone and cannot
            # await, so the stored value is cached on application.state here and
            # refreshed whenever the 系统 form saves it.
            application.state.display_timezone = (
                await application.state.system_settings_service.timezone()
            )
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
                user_client_factory=telegram_user_client_factory,
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
                    concurrency_provider=(
                        application.state.system_settings_service.source_concurrency
                    ),
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
                        lambda: _exhentai_credentials(secret_store)
                    ),
                    http_client=exhentai_client,
                    client_http_client=torrent_client,
                    poll_seconds=float(app_settings.torrent_poll_seconds),
                    work_path_provider=archive_settings_service.work_path,
                    # Read off application.state per delivery: the conversion service
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
                    lambda: _telegram_context(secret_store, telegram_client)
                ),
                # Handed out by the connection manager because it owns the
                # session: a second reader of the credential store would be a
                # second place that has to know how the api pair is encoded.
                telegram_user_client=(
                    lambda: _required(
                        application,
                        "connection_manager",
                        TELEGRAM_USER_UNAVAILABLE,
                    ).telegram_user_context()
                ),
                exhentai_download=(
                    lambda candidate_id: application.state.exhentai_service
                    .download_archive_for_candidate(candidate_id)
                ),
                # Read off `application.state` per delivery rather than captured:
                # both services are optional by configuration, and a job that routes
                # to a source this deployment does not run must fail as 503 rather
                # than as an AttributeError inside the download loop.
                telegraph_download=(
                    lambda candidate_id: _required(
                        application, "telegraph_service", TELEGRAPH_UNAVAILABLE
                    ).download_for_candidate(candidate_id)
                ),
                torrent_push=(
                    lambda candidate_id: _required(
                        application, "torrent_service", TORRENT_UNAVAILABLE
                    ).push_for_candidate(candidate_id)
                ),
                torrent_abandon=(
                    lambda job_id: _required(
                        application, "torrent_service", TORRENT_UNAVAILABLE
                    ).abandon(job_id)
                ),
                torrent_verify=(
                    lambda job_id: _required(
                        application, "torrent_service", TORRENT_UNAVAILABLE
                    ).complete_if_ready(job_id)
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
                    lambda **data: application.state.event_bus.publish(
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
                    lambda **data: application.state.event_bus.publish(
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
                    lambda: _exhentai_credentials(secret_store)
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
            application.state.startup_errors.append(str(exc))
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
    return lifespan


__all__ = ["build_lifespan", "seed_state"]
