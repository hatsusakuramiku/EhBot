import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
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
from app.config import Settings
from app.bootstrap import remove_bootstrap_password, write_bootstrap_password
from app.connections.exhentai import ExHentaiCredentials
from app.connections.manager import ConnectionManager
from app.connections.models import ProviderConnectionError
from app.db.database import Database
from app.errors import AppError, app_error_handler
from app.logging import configure_logging
from app.secrets import SecretStore
from app.storage.readiness import ensure_writable_directory


def create_app(
    settings: Settings | None = None,
    *,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
    exhentai_transport: httpx.AsyncBaseTransport | None = None,
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
            if telegram_client is not None:
                await telegram_client.aclose()
            if exhentai_client is not None:
                await exhentai_client.aclose()

    app = FastAPI(
        title="EhBot", lifespan=lifespan, root_path=app_settings.app_root_path
    )
    app.state.settings = app_settings
    app.state.database = database
    app.state.connection_manager = None
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
        return templates.TemplateResponse(
            request=request,
            name="candidates.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "candidates": await database.list_candidates(),
            },
        )

    @app.get("/candidates/{candidate_id}")
    async def candidate_detail(request: Request, candidate_id: int):
        redirect = require_authenticated(request)
        if redirect:
            return redirect
        candidate = await database.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        return templates.TemplateResponse(
            request=request,
            name="candidate_detail.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "candidate": candidate,
            },
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
