from contextlib import asynccontextmanager
import hmac
import logging
from pathlib import Path
import secrets
import sqlite3
import time

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings
from app.db.database import Database
from app.logging import configure_logging
from app.storage.readiness import ensure_writable_directory


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    app_settings = settings or Settings.from_env()
    database = Database(app_settings.data_path / "ehbot.db")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app.state.startup_errors = app_settings.readiness_errors()
        try:
            for path in (
                app_settings.data_path,
                app_settings.library_path,
                app_settings.work_path,
            ):
                ensure_writable_directory(path)
            await database.initialize()
        except (OSError, ValueError, sqlite3.Error) as exc:
            app.state.startup_errors.append(str(exc))
            logging.getLogger(__name__).error(
                "application_startup_failed", extra={"error_code": "STARTUP_FAILED"}
            )
        yield

    app = FastAPI(title="EhBot", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.database = database
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

    @app.get("/")
    async def dashboard(request: Request):
        if not request.session.get("authenticated"):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"csrf_token": request.session["csrf_token"]},
        )

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
        if not app_settings.admin_password_hash:
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        try:
            password_matches = PasswordHash.recommended().verify(
                password, app_settings.admin_password_hash
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
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form()):
        validate_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        errors = list(app.state.startup_errors)
        if not await database.check_writable():
            errors.append("database is not writable")
        if errors:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "errors": errors},
            )
        return JSONResponse(content={"status": "ready"})

    return app


app = create_app()
