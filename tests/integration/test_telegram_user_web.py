"""Logging a Telegram user account in, and using it to fetch a big attachment.

The point of the feature is the 20 MB Bot API ceiling: above it `getFile` refuses
permanently, so the same archive has to come over MTProto with the operator's own
account. These tests drive that end to end against a real app -- the login form,
the credential store, routing, and the download worker -- with a stub in place of
Telethon.

The job is seeded as PENDING here on purpose, unlike elsewhere in the suite: this
one *wants* the worker to claim it, because the worker's user-account branch is
what is under test. It is awaited through the job's own state rather than a sleep.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.connections.manager import (
    TELEGRAM_USER_API_SECRET,
    TELEGRAM_USER_SESSION_SECRET,
)
from app.db.database import Database
from app.main import create_app
from app.review.orchestration import TELEGRAM_FILE_LIMIT


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
        torrent_enabled=False,
        telegraph_enabled=False,
    )


def authenticate(client: TestClient, settings: Settings) -> None:
    bootstrap_password = (
        settings.data_path / "bootstrap_admin_password"
    ).read_text(encoding="utf-8")
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


def bot_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/getUpdates"):
        return httpx.Response(200, json={"ok": True, "result": []})
    raise AssertionError(f"Unexpected request: {request.url}")


class SessionPasswordNeededError(Exception):
    """Named to match Telethon's class, which is how it is translated."""


class PhoneCodeInvalidError(Exception):
    pass


class StubTelethonClient:
    """One stub standing in for Telethon across a whole app lifetime."""

    #: Shared so a test can inspect what the app did without reaching into the
    #: connection manager. Reset per instance construction of the factory below.
    def __init__(self, script: dict) -> None:
        self._script = script
        self.session = self
        self.downloads: list[tuple[int, int]] = []

    # -- session protocol ------------------------------------------------
    def save(self) -> str:
        return self._script["session"]

    # -- client protocol -------------------------------------------------
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return bool(self._script.get("authorized", True))

    async def send_code_request(self, phone: str):
        self._script["phone"] = phone

        class Sent:
            phone_code_hash = "hash-abc"

        return Sent()

    async def sign_in(self, **kwargs):
        if "password" in kwargs:
            if kwargs["password"] != self._script.get("password"):
                raise Exception("PasswordHashInvalidError")
            return None
        if self._script.get("needs_password"):
            self._script["needs_password"] = False
            raise SessionPasswordNeededError()
        if kwargs.get("code") != self._script.get("code"):
            raise PhoneCodeInvalidError()
        return None

    async def get_me(self):
        class Me:
            id = 777
            username = "bigfiles"
            first_name = "Big"
            last_name = "Files"

        return Me()

    async def get_messages(self, chat_id, ids):
        class Message:
            media = "document"

        self._script["requested"] = (chat_id, ids)
        return Message()

    async def download_media(self, message, file, part_size_kb, progress_callback):
        self.downloads.append(self._script["requested"])
        Path(file).write_bytes(self._script["payload"])
        return file


def make_app(tmp_path: Path, script: dict):
    settings = make_settings(tmp_path)
    stub = StubTelethonClient(script)
    app = create_app(
        settings,
        telegram_transport=httpx.MockTransport(bot_transport),
        telegram_user_client_factory=lambda api_id, api_hash, session: stub,
    )
    return app, settings, stub


def login(client: TestClient, *, code: str = "12345", password: str | None = None):
    page = client.get("/settings/connections")
    started = client.post(
        "/connections/telegram-user/login",
        data={
            "api_id": "1234567",
            "api_hash": "a" * 32,
            "phone": "+8613800138000",
            "csrf_token": page.context["csrf_token"],
        },
        follow_redirects=True,
    )
    verified = client.post(
        "/connections/telegram-user/verify",
        data={
            "code": code,
            "password": password or "",
            "csrf_token": started.context["csrf_token"],
        },
        follow_redirects=True,
    )
    return started, verified


def test_a_full_login_stores_the_session_and_reports_the_account(
    tmp_path: Path,
) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "session-blob", "payload": b"x"}
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        started, verified = login(client)
        status = client.get("/api/connections/status").json()
        final_page = client.get("/settings/connections")

    # Step one parks the login on the code, naming where it was sent.
    assert started.context["connections"]["telegram_user"]["awaiting_code"] is True
    assert (
        started.context["connections"]["telegram_user"]["phone"] == "+8613800138000"
    )
    assert verified.status_code == 200
    assert status["telegram_user"]["state"] == "connected"
    assert status["telegram_user"]["identity"] == "@bigfiles"
    # The page reads the same fact through `status.py`, so the operator sees
    # 「已连接」and not a raw literal.
    rendered = final_page.context["connections"]["telegram_user"]
    assert rendered["state"]["code"] == "connected"
    assert rendered["state"]["label"] == "已连接"
    # The session is a full account credential, so it lives in the private store
    # and nowhere else.
    stored = (settings.data_path / "private" / TELEGRAM_USER_SESSION_SECRET)
    assert stored.read_text(encoding="utf-8") == "session-blob"


def test_the_session_is_never_echoed_back_to_the_page(tmp_path: Path) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "super-secret-session", "payload": b"x"}
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        login(client)
        page = client.get("/settings/connections")
        status = client.get("/api/connections/status").text

    assert "super-secret-session" not in page.text
    assert "super-secret-session" not in status
    # The api_hash is a credential too, and the form must come back empty.
    assert "a" * 32 not in page.text


def test_a_two_factor_account_is_asked_for_its_password(tmp_path: Path) -> None:
    app, settings, _ = make_app(
        tmp_path,
        {
            "code": "12345",
            "needs_password": True,
            "password": "vault-pass",
            "session": "session-2fa",
            "payload": b"x",
        },
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/connections")
        client.post(
            "/connections/telegram-user/login",
            data={
                "api_id": "1234567",
                "api_hash": "a" * 32,
                "phone": "+8613800138000",
                "csrf_token": page.context["csrf_token"],
            },
            follow_redirects=True,
        )
        refused = client.post(
            "/connections/telegram-user/verify",
            data={"code": "12345", "password": "", "csrf_token": page.context["csrf_token"]},
        )
        awaiting = client.get("/settings/connections")
        finished = client.post(
            "/connections/telegram-user/verify",
            data={
                "code": "",
                "password": "vault-pass",
                "csrf_token": awaiting.context["csrf_token"],
            },
            follow_redirects=True,
        )
        status = client.get("/settings/connections").context["connections"]

    # A 2FA prompt is not the operator's mistake, so the login parks rather than
    # resetting: the code does not have to be requested again.
    assert refused.status_code == 400
    assert (
        awaiting.context["connections"]["telegram_user"]["awaiting_password"] is True
    )
    assert finished.status_code == 200
    assert status["telegram_user"]["state"]["code"] == "connected"
    assert status["telegram_user"]["identity"] == "@bigfiles"


def test_a_wrong_code_keeps_the_login_open(tmp_path: Path) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": b"x"}
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/connections")
        client.post(
            "/connections/telegram-user/login",
            data={
                "api_id": "1234567",
                "api_hash": "a" * 32,
                "phone": "+8613800138000",
                "csrf_token": page.context["csrf_token"],
            },
            follow_redirects=True,
        )
        refused = client.post(
            "/connections/telegram-user/verify",
            data={"code": "00000", "password": "", "csrf_token": page.context["csrf_token"]},
        )
        after = client.get("/settings/connections")

    assert refused.status_code == 400
    # Still awaiting the code, not thrown back to the api form: the code Telegram
    # sent is still valid and re-requesting one would cost the operator a wait.
    assert after.context["connections"]["telegram_user"]["awaiting_code"] is True


def test_an_empty_verification_is_refused_without_calling_telegram(
    tmp_path: Path,
) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": b"x"}
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/connections")
        refused = client.post(
            "/connections/telegram-user/verify",
            data={"code": "", "password": "", "csrf_token": page.context["csrf_token"]},
        )

    assert refused.status_code == 400


def test_disconnecting_forgets_both_secrets(tmp_path: Path) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": b"x"}
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        login(client)
        page = client.get("/settings/connections")
        client.post(
            "/connections/telegram-user/disconnect",
            data={"csrf_token": page.context["csrf_token"]},
            follow_redirects=True,
        )
        status = client.get("/settings/connections").context["connections"]

    assert status["telegram_user"]["state"]["code"] == "not_configured"
    private = settings.data_path / "private"
    # Both, not just the session: 「断开」 means this deployment no longer holds a
    # credential for that account.
    assert not (private / TELEGRAM_USER_SESSION_SECRET).exists()
    assert not (private / TELEGRAM_USER_API_SECRET).exists()


def test_the_login_forms_require_csrf(tmp_path: Path) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": b"x"}
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        bodies = {
            "/connections/telegram-user/login": {
                "api_id": "1234567",
                "api_hash": "a" * 32,
                "phone": "+8613800138000",
            },
            "/connections/telegram-user/verify": {"code": "12345"},
            "/connections/telegram-user/disconnect": {},
        }
        for path, body in bodies.items():
            # Every declared field is present, so a 403 can only be the token:
            # a missing field would be a 422 and would pass this test for the
            # wrong reason.
            response = client.post(path, data={**body, "csrf_token": "forged"})
            assert response.status_code == 403, path


async def seed_oversized_candidate(database: Database) -> int:
    """One APPROVED candidate whose only attachment is over the Bot API limit."""
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Fixture Channel",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 900,
                "message": {
                    "message_id": 5001,
                    "date": 1735689600,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "text": "[Artist] A Very Large Book",
                    "document": {
                        "file_id": "big-file",
                        "file_unique_id": "big-unique",
                        "file_name": "big.zip",
                        "mime_type": "application/zip",
                        "file_size": TELEGRAM_FILE_LIMIT * 3,
                    },
                },
            }
        ]
    )
    from app.candidates.ingestor import CandidateIngestor

    await CandidateIngestor(database).process_pending_updates()
    with sqlite3.connect(database.path) as connection:
        candidate_id = int(
            connection.execute("SELECT id FROM candidates").fetchone()[0]
        )
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    return candidate_id


def wait_for_state(database_path: Path, job_id: int, state: str) -> str:
    """Poll the job row until it leaves PENDING, then report where it landed."""
    for _ in range(200):
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT state, error_code FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is not None and str(row[0]) == state:
            return str(row[0])
        import time

        time.sleep(0.05)
    return str(row[0]) if row else "MISSING"


def test_an_oversized_attachment_is_fetched_by_the_user_account(
    tmp_path: Path,
) -> None:
    payload = b"PK\x03\x04" + b"0" * 4096
    app, settings, stub = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": payload}
    )
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_oversized_candidate(database))

    with TestClient(app) as client:
        authenticate(client, settings)
        login(client)
        page = client.get(f"/works/{candidate_id}")
        client.post(
            f"/candidates/{candidate_id}/telegram-user",
            data={"csrf_token": page.context["csrf_token"]},
            follow_redirects=True,
        )
        with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
            job = connection.execute(
                "SELECT id, provider, details_json FROM download_jobs "
                "WHERE provider = 'TELEGRAM_USER'"
            ).fetchone()
        assert job is not None, "the user-account job was never enqueued"
        state = wait_for_state(settings.data_path / "ehbot.db", int(job[0]), "COMPLETED")

    assert state == "COMPLETED"
    # Located by chat and message, because a bot `file_id` cannot be resolved by
    # a user account at all.
    details = json.loads(job[2])
    assert details["chat_id"] == -100123
    assert details["message_id"] == 5001
    assert stub.downloads == [(-100123, 5001)]
    with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
        artifact = connection.execute(
            "SELECT path, size_bytes FROM artifacts WHERE job_id = ?", (int(job[0]),)
        ).fetchone()
    assert artifact is not None
    assert int(artifact[1]) == len(payload)
    assert Path(artifact[0]).read_bytes() == payload


def test_the_work_page_offers_the_user_route_only_once_logged_in(
    tmp_path: Path,
) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": b"x"}
    )
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_oversized_candidate(database))

    with TestClient(app) as client:
        authenticate(client, settings)
        before = client.get(f"/api/v1/works/{candidate_id}").json()
        login(client)
        after = client.get(f"/api/v1/works/{candidate_id}").json()

    def entry(payload):
        return next(
            item
            for item in payload["actions"]["sources"]
            if item["provider"]["code"] == "TELEGRAM_USER"
        )

    # Always listed, so an operator can tell「没登录」from「这本没有附件」-- which is
    # the same reason every other source is shown disabled rather than hidden.
    assert entry(before)["available"] is False
    assert entry(after)["available"] is True
    assert entry(after)["action"] == f"/candidates/{candidate_id}/telegram-user"


def test_a_download_without_a_session_fails_with_a_named_reason(
    tmp_path: Path,
) -> None:
    app, settings, _ = make_app(
        tmp_path, {"code": "12345", "session": "s", "payload": b"x"}
    )
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_oversized_candidate(database))

    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        client.post(
            f"/candidates/{candidate_id}/telegram-user",
            data={"csrf_token": page.context["csrf_token"]},
            follow_redirects=True,
        )
        with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
            job_id = int(
                connection.execute(
                    "SELECT id FROM download_jobs WHERE provider = 'TELEGRAM_USER'"
                ).fetchone()[0]
            )
        wait_for_state(settings.data_path / "ehbot.db", job_id, "FAILED")
        with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
            state, code = connection.execute(
                "SELECT state, error_code FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()

    assert state == "FAILED"
    # Named, because「没登录」is fixable by the operator and a generic download
    # failure would not tell them that.
    assert code == "TELEGRAM_USER_NOT_CONFIG"
