import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.routes.auth import (
    LOCKOUT_SECONDS,
    MAX_FAILED_ATTEMPTS,
    MAX_TRACKED_CLIENTS,
    _prune_expired,
)
from app.web.security_headers import SECURITY_HEADERS


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
    )


def read_bootstrap_password(settings: Settings) -> str:
    return (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )


def log_in(client: TestClient, password: str):
    login_page = client.get("/login")
    return client.post(
        "/login",
        data={
            "password": password,
            "csrf_token": login_page.context["csrf_token"],
        },
        follow_redirects=False,
    )


def test_anonymous_user_is_redirected_to_login(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_root_path_is_included_in_generated_web_urls(tmp_path: Path) -> None:
    base_settings = make_settings(tmp_path)
    settings = Settings(
        data_path=base_settings.data_path,
        library_path=base_settings.library_path,
        work_path=base_settings.work_path,
        app_secret_key=base_settings.app_secret_key,
        app_root_path="/ehbot",
    )
    with TestClient(create_app(settings)) as client:
        redirect = client.get("/", follow_redirects=False)
        login_page = client.get("/login")

    assert redirect.headers["location"] == "/ehbot/login"
    assert 'href="http://testserver/ehbot/static/ui.css"' in login_page.text


def test_login_rejects_invalid_csrf_token(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.get("/login")
        response = client.post(
            "/login",
            data={"password": "correct-password", "csrf_token": "invalid"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid CSRF token"}


def test_bootstrap_password_requires_an_immediate_password_change(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = log_in(client, read_bootstrap_password(settings))
        protected_page = client.get("/", follow_redirects=False)
        change_page = client.get("/settings/passwords")

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/passwords"
    assert protected_page.headers["location"] == "/settings/passwords"
    assert change_page.status_code == 200
    assert "管理员密码" in change_page.text


def test_changing_password_removes_bootstrap_file_and_stops_rotation(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    new_password = "new-password-with-12-characters"

    with TestClient(create_app(settings)) as client:
        bootstrap_password = read_bootstrap_password(settings)
        log_in(client, bootstrap_password)
        change_page = client.get("/settings/passwords")
        response = client.post(
            "/change-password",
            data={
                "current_password": bootstrap_password,
                "new_password": new_password,
                "confirmation": new_password,
                "csrf_token": change_page.context["csrf_token"],
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert not (settings.data_path / "bootstrap_admin_password").exists()

    with TestClient(create_app(settings)) as restarted_client:
        assert not (settings.data_path / "bootstrap_admin_password").exists()
        login_response = log_in(restarted_client, new_password)
        dashboard = restarted_client.get("/")

    assert login_response.headers["location"] == "/"
    assert dashboard.status_code == 200
    assert "待审核" in dashboard.text


def test_unmodified_bootstrap_password_rotates_on_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)):
        first_password = read_bootstrap_password(settings)

    with TestClient(create_app(settings)) as restarted_client:
        second_password = read_bootstrap_password(settings)
        old_password_response = log_in(restarted_client, first_password)
        new_password_response = log_in(restarted_client, second_password)

    assert second_password != first_password
    assert old_password_response.status_code == 401
    assert new_password_response.headers["location"] == "/settings/passwords"


def test_authenticated_user_can_log_out(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, read_bootstrap_password(settings))
        change_page = client.get("/settings/passwords")
        response = client.post(
            "/logout",
            data={"csrf_token": change_page.context["csrf_token"]},
            follow_redirects=False,
        )
        protected_page = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert protected_page.status_code == 303


def test_repeated_login_failures_are_temporarily_blocked(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        login_page = client.get("/login")
        form = {
            "password": "wrong-password",
            "csrf_token": login_page.context["csrf_token"],
        }
        responses = [client.post("/login", data=form) for _ in range(6)]

    assert [response.status_code for response in responses] == [
        401,
        401,
        401,
        401,
        401,
        429,
    ]
    assert responses[-1].json() == {"detail": "Too many login attempts"}


def test_a_lockout_is_recorded_in_the_log(tmp_path: Path, caplog) -> None:
    """A locked-out operator must be able to find out why from the log.

    The throttle used to fire silently: the only trace was a 429 in the access
    log, which says a request was refused and not that this address had spent
    its attempts.
    """
    with TestClient(create_app(make_settings(tmp_path))) as client:
        login_page = client.get("/login")
        form = {
            "password": "wrong-password",
            "csrf_token": login_page.context["csrf_token"],
        }
        with caplog.at_level(logging.WARNING, logger="app.web.routes.auth"):
            for _ in range(MAX_FAILED_ATTEMPTS):
                client.post("/login", data=form)

    locked = [r for r in caplog.records if r.message.startswith("login_locked_out")]
    assert len(locked) == 1
    assert getattr(locked[0], "error_code", None) == "LOGIN_LOCKED_OUT"


def test_the_attempt_table_does_not_grow_without_bound() -> None:
    """One entry per address, kept only as long as it means something.

    Entries were removed on a successful login or when the same address came
    back after its lock expired, so an address that failed once and never
    returned stayed for the life of the process.
    """
    now = 1000.0
    attempts = {
        "expired": (MAX_FAILED_ATTEMPTS, now - 1),
        "still-locked": (MAX_FAILED_ATTEMPTS, now + LOCKOUT_SECONDS),
        "counting": (1, 0.0),
    }
    _prune_expired(attempts, now)
    assert set(attempts) == {"still-locked", "counting"}

    # A spray from more addresses than the ceiling sheds the oldest rather
    # than growing without limit.
    flood = {f"host-{index}": (1, 0.0) for index in range(MAX_TRACKED_CLIENTS + 50)}
    _prune_expired(flood, now)
    assert len(flood) == MAX_TRACKED_CLIENTS


def test_every_response_carries_the_security_headers(tmp_path: Path) -> None:
    """Set once in middleware, so no route can be the one that forgot.

    Checked on a page, on a JSON endpoint and on a redirect, because those are
    three different response objects and only the middleware sees all three.
    """
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        page = client.get("/login")
        api = client.get("/api/v1/summary")
        redirect = client.get("/", follow_redirects=False)

    for response in (page, api, redirect):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value


def test_the_policy_confines_the_page_to_this_origin(tmp_path: Path) -> None:
    """The part of the CSP that is actually enforced.

    `script-src` has to allow inline and eval (the pre-paint theme bootstrap
    and Alpine's expression evaluation), so the value of the policy is in the
    other directives: nothing may be loaded from, or sent to, another origin,
    and the page may not be framed.
    """
    with TestClient(create_app(make_settings(tmp_path))) as client:
        policy = client.get("/login").headers["Content-Security-Policy"]

    assert "default-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert "form-action 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy


def test_bootstrap_password_is_printed_to_the_console(
    tmp_path: Path, capsys
) -> None:
    """The first-run password must be readable from the console.

    Operators run this in Docker, where the data directory is a bind mount or a
    volume. Telling them to open a file inside the container is not a usable
    handover, so `docker compose logs` has to show the value itself.
    """
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)):
        password = read_bootstrap_password(settings)

    printed = capsys.readouterr().out
    assert password in printed
    assert "admin" in printed
    assert str(settings.data_path / "bootstrap_admin_password") in printed


def test_console_banner_is_absent_once_the_password_is_changed(
    tmp_path: Path, capsys
) -> None:
    """A steady-state restart must not reprint credentials."""
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        password = read_bootstrap_password(settings)
        log_in(client, password)
        change_page = client.get("/settings/passwords")
        new_password = "rotated-console-password-2026"
        client.post(
            "/change-password",
            data={
                "current_password": password,
                "new_password": new_password,
                "confirmation": new_password,
                "csrf_token": change_page.context["csrf_token"],
            },
            follow_redirects=False,
        )

    capsys.readouterr()
    with TestClient(create_app(settings)):
        pass

    restart_output = capsys.readouterr().out
    assert new_password not in restart_output
    assert "\u521d\u59cb\u5bc6\u7801" not in restart_output
