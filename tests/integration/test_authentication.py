from pathlib import Path

from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from app.config import Settings
from app.main import create_app


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        admin_password_hash=PasswordHash.recommended().hash("correct-password"),
    )


def test_anonymous_user_is_redirected_to_login(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_rejects_invalid_csrf_token(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.get("/login")
        response = client.post(
            "/login",
            data={"password": "correct-password", "csrf_token": "invalid"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid CSRF token"}


def test_user_can_log_in_with_configured_password(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        login_page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "password": "correct-password",
                "csrf_token": login_page.context["csrf_token"],
            },
            follow_redirects=False,
        )
        dashboard = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert dashboard.status_code == 200
    assert "待审核" in dashboard.text


def test_authenticated_user_can_log_out(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        login_page = client.get("/login")
        client.post(
            "/login",
            data={
                "password": "correct-password",
                "csrf_token": login_page.context["csrf_token"],
            },
        )
        dashboard = client.get("/")
        response = client.post(
            "/logout",
            data={"csrf_token": dashboard.context["csrf_token"]},
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
