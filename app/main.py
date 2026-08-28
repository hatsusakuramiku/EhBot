"""Application assembly: build one `FastAPI`, wire it, mount the routers.

Everything this file used to do itself now lives beside what it serves --
startup in `app.wiring`, the Jinja environment in `app.web.rendering`, the
page-layer dependencies in `app.web.deps`, and each domain's handlers in
`app.web.routes`. What is left is the order those pieces go together in, which
is the one thing that genuinely belongs to the application as a whole.

Router order is load-bearing. Starlette matches in declaration order, so a
router whose paths could shadow another's has to be included after it; the
comment on each `include_router` says what its position is protecting.
"""

from fastapi import FastAPI
import httpx
from fastapi.staticfiles import StaticFiles
from pwdlib import PasswordHash
from starlette.middleware.sessions import SessionMiddleware

from app.api.contracts import ApiError, api_error_handler
from app.api.v1 import router as api_v1_router
from app.config import Settings
from app.db.database import Database
from app.errors import AppError, app_error_handler
from app.logging import configure_logging
from app.session_secret import resolve_session_secret
from app.web.rendering import STATIC_DIR, build_templates
from app.web.routes.activity import router as activity_router
from app.web.routes.auth import router as auth_router
from app.web.routes.auto_approval import router as auto_approval_router
from app.web.routes.candidates import router as candidates_router
from app.web.routes.connections import router as connections_router
from app.web.routes.dashboard import router as dashboard_router
from app.web.routes.downloaded import router as downloaded_router
from app.web.routes.health import router as health_router
from app.web.routes.manual_add import router as manual_add_router
from app.web.routes.settings_pages import router as settings_router
from app.web.routes.ui_kit import router as ui_kit_router
from app.web.routes.works import router as works_router
from app.wiring import build_lifespan, seed_state


def create_app(
    settings: Settings | None = None,
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

    app = FastAPI(
        title="EhBot",
        lifespan=build_lifespan(
            app_settings,
            database,
            password_hasher,
            session_secret,
            telegram_transport=telegram_transport,
            telegram_user_client_factory=telegram_user_client_factory,
            exhentai_transport=exhentai_transport,
            tagdb_transport=tagdb_transport,
            telegraph_transport=telegraph_transport,
            telegraph_resolver=telegraph_resolver,
            torrent_client_transport=torrent_client_transport,
            thumbnail_transport=thumbnail_transport,
            thumbnail_resolver=thumbnail_resolver,
        ),
        root_path=app_settings.app_root_path,
    )
    seed_state(app, app_settings, database)
    # Published on `app.state` rather than captured in a closure: the route
    # modules never see `create_app`'s locals, only a request.
    app.state.password_hasher = password_hasher
    # Failed-login counter, per application rather than per module, so two
    # applications in one test session cannot share a lockout.
    app.state.login_attempts = {}
    app.state.templates = build_templates()
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret.key,
        https_only=app_settings.session_cookie_secure,
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # The JSON API first: every one of its paths is under `/api/v1`, so it can
    # shadow nothing below and nothing below can shadow it.
    app.include_router(api_v1_router)
    app.include_router(auto_approval_router)
    app.include_router(dashboard_router)
    # Above `works` and `manual_add` only for readability -- no shared prefix.
    # The order that matters is inside this router, where the six literal tabs
    # are declared above `/candidates/{candidate_id}`.
    app.include_router(candidates_router)
    app.include_router(works_router)
    app.include_router(manual_add_router)
    app.include_router(activity_router)
    # Its five literal tabs are declared above `/downloaded/{candidate_id}/...`
    # inside the module, which is the order that matters; the position here only
    # keeps it beside the other domain pages.
    app.include_router(downloaded_router)
    app.include_router(settings_router)
    app.include_router(ui_kit_router)
    app.include_router(connections_router)
    app.include_router(auth_router)
    app.include_router(health_router)
    return app


app = create_app()
