"""Integration tests for the single mobile API key and the token TTLs.

The key is minted in the 密码库 tab, shown exactly once, used directly as a
bearer credential, and revoked/refreshed from the same panel. Two invariants get
their own tests because they are easy to regress: at most one valid key exists
at any time, and changing the administrator password does not touch it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


NEW_PASSWORD = "a-much-longer-operator-password"
ROTATED_PASSWORD = "yet-another-operator-password"

#: The plaintext is what the reveal page renders; this pulls it back out.
_KEY_PATTERN = re.compile(r"ehk_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
    )


def complete_first_run(client: TestClient, settings: Settings) -> None:
    password = (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )
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


def generate_key(client: TestClient) -> str:
    """Generate a key, follow the reveal redirect, and return the plaintext."""
    page = client.get("/settings/passwords")
    response = client.post(
        "/settings/api-keys/generate",
        data={"csrf_token": page.context["csrf_token"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    reveal = client.get(response.headers["location"])
    match = _KEY_PATTERN.search(reveal.text)
    assert match is not None, "the reveal page did not show a key"
    return match.group(0)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def count_valid_keys(settings: Settings) -> int:
    connection = sqlite3.connect(settings.data_path / "ehbot.db")
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM api_credentials "
                "WHERE kind = 'api_key' AND revoked_at IS NULL"
            ).fetchone()[0]
        )
    finally:
        connection.close()


class TestKeyLifecycle:
    def test_a_fresh_deployment_has_no_key(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            page = client.get("/settings/passwords")
            snapshot = client.get("/api/v1/settings/passwords").json()

        assert "生成 API 密钥" in page.text
        assert "未生成" in page.text
        assert snapshot["api_key"] == {"configured": False}
        assert count_valid_keys(settings) == 0

    def test_the_key_is_shown_exactly_once(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            key = generate_key(client)
            reloaded = client.get("/settings/passwords")

        assert _KEY_PATTERN.match(key)
        assert key not in reloaded.text
        assert "已生成" in reloaded.text

    def test_the_metadata_never_carries_the_secret(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            key = generate_key(client)
            snapshot = client.get("/api/v1/settings/passwords").json()

        serialized = str(snapshot)
        assert key not in serialized
        assert key.split(".")[1] not in serialized
        api_key = snapshot["api_key"]
        assert api_key["configured"] is True
        assert api_key["public_id"] == key.split(".")[0]
        assert api_key["label"] == "移动端"
        assert "secret" not in serialized.lower()
        assert "hash" not in serialized.lower()

    def test_generating_again_invalidates_the_old_key(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            first = generate_key(client)
            second = generate_key(client)

            old = client.get("/api/v1/meta", headers=bearer(first))
            new = client.get("/api/v1/meta", headers=bearer(second))

        assert old.status_code == 401
        assert old.json()["error"]["code"] == "AUTH_TOKEN_REVOKED"
        assert new.status_code == 200
        assert count_valid_keys(settings) == 1

    def test_revoking_leaves_no_valid_key(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            key = generate_key(client)

            page = client.get("/settings/passwords")
            revoked = client.post(
                "/settings/api-keys/revoke",
                data={"csrf_token": page.context["csrf_token"]},
                follow_redirects=False,
            )
            after = client.get("/api/v1/meta", headers=bearer(key))
            snapshot = client.get("/api/v1/settings/passwords").json()

        assert revoked.status_code == 303
        assert after.status_code == 401
        assert after.json()["error"]["code"] == "AUTH_TOKEN_REVOKED"
        assert snapshot["api_key"] == {"configured": False}
        assert count_valid_keys(settings) == 0

    def test_a_key_can_be_generated_again_after_revocation(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            generate_key(client)
            page = client.get("/settings/passwords")
            client.post(
                "/settings/api-keys/revoke",
                data={"csrf_token": page.context["csrf_token"]},
                follow_redirects=False,
            )
            replacement = generate_key(client)
            response = client.get("/api/v1/meta", headers=bearer(replacement))

        assert response.status_code == 200
        assert count_valid_keys(settings) == 1


class TestKeyUsage:
    def test_the_key_authenticates_asks_and_skips_csrf(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            key = generate_key(client)

            whoami = client.get("/api/v1/auth/whoami", headers=bearer(key))
            # A write with no CSRF header, which a cookie session would refuse.
            write = client.post(
                "/api/v1/candidates/batch",
                json={"action": "approve", "candidate_ids": [1]},
                headers=bearer(key),
            )

        assert whoami.status_code == 200
        assert whoami.json()["kind"] == "api_key"
        assert whoami.json()["label"] == "移动端"
        assert write.json().get("error", {}).get("code") != "CSRF_INVALID"

    def test_changing_the_password_keeps_the_key(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            key = generate_key(client)

            page = client.get("/settings/passwords")
            client.post(
                "/change-password",
                data={
                    "current_password": NEW_PASSWORD,
                    "new_password": ROTATED_PASSWORD,
                    "confirmation": ROTATED_PASSWORD,
                    "csrf_token": page.context["csrf_token"],
                },
                follow_redirects=False,
            )
            after = client.get("/api/v1/meta", headers=bearer(key))

        assert after.status_code == 200

    def test_an_api_key_is_not_a_password_session(self, tmp_path: Path) -> None:
        """Logging out an access token must not revoke the operator's key."""
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            key = generate_key(client)
            access = client.post(
                "/api/v1/auth/login", json={"password": NEW_PASSWORD}
            ).json()["access_token"]

            client.post("/api/v1/auth/logout", headers=bearer(access))
            key_still_works = client.get("/api/v1/meta", headers=bearer(key))

        assert key_still_works.status_code == 200


class TestKeyAuthorization:
    def test_minting_requires_a_session(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/settings/api-keys/generate",
                data={"csrf_token": "anything"},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_minting_requires_the_csrf_token(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            response = client.post(
                "/settings/api-keys/generate",
                data={"csrf_token": "wrong"},
                follow_redirects=False,
            )

        assert response.status_code == 403


class TestTokenTtlSettings:
    def test_saved_ttls_reach_a_login(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            page = client.get("/settings/system")
            saved = client.post(
                "/settings/system",
                data={
                    "csrf_token": page.context["csrf_token"],
                    "mobile_access_ttl_seconds": "3600",
                    "mobile_refresh_ttl_seconds": "604800",
                },
                follow_redirects=False,
            )
            login = client.post(
                "/api/v1/auth/login", json={"password": NEW_PASSWORD}
            )

        assert saved.status_code == 303
        assert login.status_code == 200
        assert login.json()["expires_in"] == 3600
        assert login.json()["refresh_expires_in"] == 604800

    def test_an_out_of_bounds_ttl_is_refused_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            complete_first_run(client, settings)
            page = client.get("/settings/system")
            response = client.post(
                "/settings/system",
                data={
                    "csrf_token": page.context["csrf_token"],
                    "mobile_access_ttl_seconds": "1",
                },
                follow_redirects=False,
            )

        assert response.status_code == 400
        assert "登录有效期" in response.text
