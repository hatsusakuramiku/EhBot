from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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
    assert 'href="http://testserver/ehbot/static/app.css"' in login_page.text


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
        change_page = client.get("/change-password")

    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"
    assert protected_page.headers["location"] == "/change-password"
    assert change_page.status_code == 200
    assert "修改密码" in change_page.text


def test_changing_password_removes_bootstrap_file_and_stops_rotation(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    new_password = "new-password-with-12-characters"

    with TestClient(create_app(settings)) as client:
        bootstrap_password = read_bootstrap_password(settings)
        log_in(client, bootstrap_password)
        change_page = client.get("/change-password")
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
    assert new_password_response.headers["location"] == "/change-password"


def test_authenticated_user_can_log_out(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, read_bootstrap_password(settings))
        change_page = client.get("/change-password")
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
        change_page = client.get("/change-password")
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
