"""Integration tests for the bearer credential endpoints.

`POST /api/v1/auth/login|refresh|logout` and `GET /api/v1/auth/whoami`, plus the
way `app/api/deps.py` lets a bearer credential reach every other `/api/v1`
endpoint while leaving the browser session path untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.config import Settings
from app.main import create_app


NEW_PASSWORD = "a-much-longer-operator-password"
ROTATED_PASSWORD = "yet-another-operator-password"


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


def complete_first_run(client: TestClient, settings: Settings) -> str:
    """Sign in with the bootstrap password and set a real one.

    Returns the password a mobile client can now use. Leaves the browser session
    authenticated; a test that wants to look anonymous clears the cookie jar.
    """
    password = read_bootstrap_password(settings)
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": password,
            "csrf_token": login_page.context["csrf_token"],
        },
        follow_redirects=False,
    )
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": password,
            "new_password": NEW_PASSWORD,
            "confirmation": NEW_PASSWORD,
            "csrf_token": change_page.context["csrf_token"],
        },
        follow_redirects=False,
    )
    return NEW_PASSWORD


def api_login(client: TestClient, password: str, label: str | None = None):
    payload: dict[str, str] = {"password": password}
    if label is not None:
        payload["label"] = label
    return client.post("/api/v1/auth/login", json=payload)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(response) -> str:
    return response.json()["error"]["code"]


class TestLogin:
    def test_login_issues_a_usable_pair(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()

            response = api_login(client, password, label="Pixel 9")

        assert response.status_code == 200
        payload = response.json()
        assert payload["token_type"] == "Bearer"
        assert payload["expires_in"] == 12 * 3600
        assert payload["refresh_expires_in"] == 30 * 86400
        assert payload["access_token"].startswith("eha_")
        assert payload["refresh_token"].startswith("ehr_")
        assert payload["access_token"] != payload["refresh_token"]

    def test_a_bearer_reaches_the_api_without_a_cookie(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            token = api_login(client, password).json()["access_token"]

            authorized = client.get("/api/v1/meta", headers=bearer(token))
            anonymous = client.get("/api/v1/meta")

        assert authorized.status_code == 200
        assert anonymous.status_code == 401
        assert error_code(anonymous) == "NOT_AUTHENTICATED"

    def test_login_is_refused_while_the_bootstrap_password_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            bootstrap = read_bootstrap_password(settings)
            client.cookies.clear()
            response = api_login(client, bootstrap)

        assert response.status_code == 403
        assert error_code(response) == "PASSWORD_CHANGE_REQUIRED"

    def test_a_wrong_password_is_401(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            client.cookies.clear()
            response = api_login(client, "not-the-password")

        assert response.status_code == 401
        assert error_code(response) == "AUTH_INVALID_CREDENTIALS"

    def test_an_empty_password_is_refused_without_a_hash_check(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            client.cookies.clear()
            response = api_login(client, "")

        assert response.status_code == 401
        assert error_code(response) == "AUTH_INVALID_CREDENTIALS"


class TestThrottle:
    def test_the_api_login_shares_the_page_throttle(self, tmp_path: Path) -> None:
        """Switching endpoints must not buy a fresh attempt budget."""
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            statuses = [
                api_login(client, "wrong-password").status_code for _ in range(6)
            ]
            # The page login now sees the same lockout, even with the right
            # password.
            login_page = client.get("/login")
            page = client.post(
                "/login",
                data={
                    "password": password,
                    "csrf_token": login_page.context["csrf_token"],
                },
                follow_redirects=False,
            )

        assert statuses == [401, 401, 401, 401, 401, 429]
        assert page.status_code == 429


class TestBearerGuard:
    def test_a_malformed_header_is_401(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            client.cookies.clear()
            response = client.get(
                "/api/v1/meta", headers={"Authorization": "Bearer not-a-token"}
            )

        assert response.status_code == 401
        assert error_code(response) == "AUTH_INVALID_CREDENTIALS"

    def test_a_well_shaped_token_nobody_issued_is_401(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            client.cookies.clear()
            response = client.get(
                "/api/v1/meta",
                headers=bearer("eha_AAAAAAAAAAAAAAAA.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            )

        assert response.status_code == 401
        assert error_code(response) == "AUTH_INVALID_CREDENTIALS"

    def test_a_refresh_token_is_not_a_bearer_credential(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            tokens = api_login(client, password).json()
            response = client.get(
                "/api/v1/meta", headers=bearer(tokens["refresh_token"])
            )

        assert response.status_code == 401
        assert error_code(response) == "AUTH_INVALID_CREDENTIALS"

    def test_an_expired_access_token_reports_expiry(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            token = api_login(client, password).json()["access_token"]

            # Age the row directly: there is no clock to advance from a test.
            connection = sqlite3.connect(settings.data_path / "ehbot.db")
            connection.execute(
                "UPDATE api_credentials SET expires_at = 1 "
                "WHERE kind = 'access' AND revoked_at IS NULL"
            )
            connection.commit()
            connection.close()

            response = client.get("/api/v1/meta", headers=bearer(token))

        assert response.status_code == 401
        assert error_code(response) == "AUTH_TOKEN_EXPIRED"

    def test_bearer_skips_csrf_while_the_cookie_session_requires_it(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        body = {"action": "approve", "candidate_ids": [1]}
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)

            # The browser session still owes the header...
            session_call = client.post("/api/v1/candidates/batch", json=body)
            # ...a bearer credential does not.
            token = api_login(client, password).json()["access_token"]
            bearer_call = client.post(
                "/api/v1/candidates/batch", json=body, headers=bearer(token)
            )

        assert session_call.status_code == 403
        assert error_code(session_call) == "CSRF_INVALID"
        # The bearer call may fail for a domain reason, but never CSRF:
        # there is no cookie for a cross-site request to borrow.
        assert bearer_call.json().get("error", {}).get("code") != "CSRF_INVALID"


class TestRefreshAndLogout:
    def test_refresh_rotates_and_the_old_token_dies(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            first = api_login(client, password).json()

            refreshed = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": first["refresh_token"]},
            )
            assert refreshed.status_code == 200
            second = refreshed.json()

            new_access_works = client.get(
                "/api/v1/meta", headers=bearer(second["access_token"])
            )
            reused = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": first["refresh_token"]},
            )

        assert new_access_works.status_code == 200
        assert reused.status_code == 401
        assert error_code(reused) == "AUTH_TOKEN_REVOKED"

    def test_logout_revokes_the_whole_family(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            tokens = api_login(client, password).json()

            logout = client.post(
                "/api/v1/auth/logout", headers=bearer(tokens["access_token"])
            )
            after_access = client.get(
                "/api/v1/meta", headers=bearer(tokens["access_token"])
            )
            after_refresh = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
            )

        assert logout.status_code == 200
        assert after_access.status_code == 401
        assert error_code(after_access) == "AUTH_TOKEN_REVOKED"
        assert after_refresh.status_code == 401
        assert error_code(after_refresh) == "AUTH_TOKEN_REVOKED"

    def test_logout_without_a_bearer_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            response = client.post("/api/v1/auth/logout")

        assert response.status_code == 401
        assert error_code(response) == "NOT_AUTHENTICATED"

    def test_whoami_names_the_credential_kind(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = complete_first_run(client, settings)
            client.cookies.clear()
            tokens = api_login(client, password, label="Pixel 9").json()
            response = client.get(
                "/api/v1/auth/whoami", headers=bearer(tokens["access_token"])
            )

        assert response.status_code == 200
        assert response.json() == {
            "username": "admin",
            "kind": "access",
            "label": "Pixel 9",
            "expires_at": response.json()["expires_at"],
            "must_change_password": False,
        }


class TestPasswordChange:
    def test_changing_the_password_revokes_mobile_tokens(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            tokens = api_login(client, NEW_PASSWORD).json()

            change_page = client.get("/settings/passwords")
            rotated = client.post(
                "/change-password",
                data={
                    "current_password": NEW_PASSWORD,
                    "new_password": ROTATED_PASSWORD,
                    "confirmation": ROTATED_PASSWORD,
                    "csrf_token": change_page.context["csrf_token"],
                },
                follow_redirects=False,
            )
            stale = client.get(
                "/api/v1/meta", headers=bearer(tokens["access_token"])
            )
            stale_refresh = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
            )
            client.cookies.clear()
            relogin = api_login(client, ROTATED_PASSWORD)

        assert rotated.status_code == 303
        assert stale.status_code == 401
        assert error_code(stale) == "AUTH_TOKEN_REVOKED"
        assert stale_refresh.status_code == 401
        assert error_code(stale_refresh) == "AUTH_TOKEN_REVOKED"
        assert relogin.status_code == 200
