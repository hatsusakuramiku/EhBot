"""Manual add: an ExHentai link, validated by fetching the gallery (R63).

The page changed meaning in R63. It used to accept a magnet too and treated the
connection state as a note; it now takes one shape (a gallery link), refuses to
work without a connection, and proves the link resolves *before* it creates a
candidate. A link that does not resolve leaves no row, which is the half of the
behaviour a URL-shape check alone cannot give.
"""

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.config import Settings
from app.db.database import Database
from app.main import create_app


def _settings(root) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
    )


def _authenticate(client: TestClient, settings: Settings) -> None:
    password = (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )
    login = client.get("/login")
    client.post(
        "/login",
        data={"password": password, "csrf_token": login.context["csrf_token"]},
    )
    change = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change.context["csrf_token"],
        },
    )


def _post(client: TestClient, url: str, data: dict) -> TestClient:
    page = client.get("/manual-add")
    data["csrf_token"] = page.context["csrf_token"]
    return client.post(url, data=data, follow_redirects=False)


def _configure_exhentai(client: TestClient, monkeypatch) -> None:
    """Make the ExHentai connection read as configured, without a Cookie.

    The gate is a `snapshot()` read, so the fake is one attribute deep -- the
    rest of the connection state is irrelevant to this page.
    """
    manager = client.app.state.connection_manager
    monkeypatch.setattr(
        manager,
        "snapshot",
        lambda: SimpleNamespace(
            exhentai=SimpleNamespace(configured=True)
        ),
    )


def _stub_gallery_fetch(
    client: TestClient,
    monkeypatch,
    *,
    metadata: dict | None = None,
    error: Exception | None = None,
) -> list[tuple[int, str]]:
    """Stub the two halves of a scrape the route drives.

    `fetch_metadata_for_gallery` validates, `persist_gallery_metadata` writes;
    the route calls them in that order with nothing in between but the insert,
    which is exactly what these fakes let a test observe: the fetched gallery is
    the one persisted, and no second request is made.
    """
    service = client.app.state.exhentai_service
    fetched: list[tuple[int, str]] = []
    persisted: list[int] = []

    async def fake_fetch(gid: int, token: str):
        if error is not None:
            raise error
        fetched.append((gid, token))
        return (metadata or {}, None)

    async def fake_persist(candidate_id: int, meta, gallery) -> None:
        persisted.append(candidate_id)
        if metadata:
            await Database(
                client.app.state.settings.data_path / "ehbot.db"
            ).set_candidate_metadata_value(
                candidate_id, "Title", metadata["Title"], "EXHENTAI", 0.6
            )

    monkeypatch.setattr(service, "fetch_metadata_for_gallery", fake_fetch)
    monkeypatch.setattr(service, "persist_gallery_metadata", fake_persist)
    return fetched


def _no_candidate_count(settings: Settings) -> int:
    import sqlite3

    with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        )


def test_manual_add_page_renders(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        page = client.get("/manual-add")
        assert page.status_code == 200
        assert "手动添加任务" in page.text
        # Unconfigured: the page says what to fix and the button cannot be
        # pressed. The server refuses the POST too (below), because a disabled
        # attribute is a UI state and not an authorisation.
        assert "未配置 Exhentai 连接" in page.text
        assert "disabled" in page.text


def test_manual_add_page_enables_the_button_when_configured(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        page = client.get("/manual-add")
        assert page.status_code == 200
        assert "未配置 Exhentai 连接" not in page.text
        assert "disabled" not in page.text


def test_submitting_without_a_connection_is_refused_and_creates_nothing(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        fetched = _stub_gallery_fetch(client, monkeypatch)
        response = _post(
            client,
            "/manual-add",
            {"input": "https://exhentai.org/g/987654/appletoken99/"},
        )
        assert response.status_code == 400
        assert "未配置 Exhentai 连接" in response.text
        # Refused before the link was even looked at: no fetch, no row.
        assert fetched == []
        assert _no_candidate_count(settings) == 0


def test_manual_add_rejects_unrecognised_input(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        fetched = _stub_gallery_fetch(client, monkeypatch)
        response = _post(client, "/manual-add", {"input": "https://example.com/thing"})
        assert response.status_code == 400
        assert "仅支持 ExHentai 画廊链接" in response.text
        assert fetched == []
        assert _no_candidate_count(settings) == 0


def test_a_magnet_is_no_longer_accepted(tmp_path, monkeypatch) -> None:
    """R63 removed the magnet input: it has a gallery neither to read nor to
    deduplicate by, which is what this page is for."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        response = _post(
            client,
            "/manual-add",
            {
                "input": (
                    "magnet:?xt=urn:btih:"
                    "0123456789abcdef0123456789abcdef01234567&dn=test"
                )
            },
        )
        assert response.status_code == 400
        assert "仅支持 ExHentai 画廊链接" in response.text
        assert _no_candidate_count(settings) == 0


def test_a_link_that_does_not_resolve_creates_no_candidate(
    tmp_path, monkeypatch
) -> None:
    """The point of fetching before inserting: 「此画廊不存在」 must not leave a
    half-made work behind for a human to clean up."""
    from app.exhentai.service import ExHentaiDownloadError

    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        _stub_gallery_fetch(
            client,
            monkeypatch,
            metadata=None,
            error=ExHentaiDownloadError("GALLERY_NOT_FOUND", "画廊不存在"),
        )
        response = _post(
            client,
            "/manual-add",
            {"input": "https://exhentai.org/g/987654/appletoken99/"},
        )
        assert response.status_code == 400
        assert "画廊不存在" in response.text
        assert _no_candidate_count(settings) == 0


def test_a_valid_eh_link_creates_an_approved_candidate_with_metadata(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        fetched = _stub_gallery_fetch(
            client, monkeypatch, metadata={"Title": "画廊标题"}
        )
        page = client.get("/manual-add")
        response = client.post(
            "/manual-add",
            data={
                "input": "https://exhentai.org/g/987654/appletoken99/",
                "csrf_token": page.context["csrf_token"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/works/1"
        # One fetch, for the gallery that was pasted.
        assert fetched == [(987654, "appletoken99")]

        database = Database(settings.data_path / "ehbot.db")
        candidate = asyncio.run(database.get_candidate(1))
        assert candidate is not None
        assert candidate.status == "APPROVED"
        assert candidate.ex_gid == 987654
        assert candidate.ex_gallery_token == "appletoken99"
        # Manual add is deliberately an exception to 「无标题进待补充」: the
        # operator asked for this one, and the gallery's own name is what the
        # metadata step wrote.
        assert candidate.title == "画廊标题"
        assert candidate.filter_reason == "手动添加：ExHentai 画廊链接"


def test_a_preview_link_is_accepted_by_manual_add(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        _stub_gallery_fetch(client, monkeypatch)
        response = _post(
            client,
            "/manual-add",
            {"input": "https://telegra.ph/ehentai-4137423-9ce70ddc26-10-06"},
        )
        assert response.status_code == 303
        candidate_id = int(
            response.headers["location"].rstrip("/").rsplit("/", 1)[-1]
        )
        database = Database(settings.data_path / "ehbot.db")
        candidate = asyncio.run(database.get_candidate(candidate_id))
        assert candidate is not None
        assert candidate.ex_gid == 4137423
        assert candidate.ex_gallery_token == "9ce70ddc26"


def test_pasting_the_same_gallery_twice_is_refused_with_a_link(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        fetched = _stub_gallery_fetch(client, monkeypatch)

        url = "https://exhentai.org/g/987654/appletoken99/"
        first = _post(client, "/manual-add", {"input": url})
        assert first.status_code == 303
        candidate_id = int(
            first.headers["location"].rstrip("/").rsplit("/", 1)[-1]
        )

        second = _post(client, "/manual-add", {"input": url})
        assert second.status_code == 409
        # The card names the work and offers it, rather than only complaining.
        assert f"/works/{candidate_id}" in second.text
        assert f"#{candidate_id}" in second.text
        assert second.headers.get("location") is None
        # The duplicate was caught before the gallery read: one fetch, for the
        # first paste only.
        assert fetched == [(987654, "appletoken99")]
        assert _no_candidate_count(settings) == 1


def test_a_different_token_for_the_same_gallery_is_still_a_duplicate(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        _stub_gallery_fetch(client, monkeypatch)

        assert _post(
            client,
            "/manual-add",
            {"input": "https://exhentai.org/g/987654/appletoken99/"},
        ).status_code == 303
        second = _post(
            client,
            "/manual-add",
            {"input": "https://exhentai.org/g/987654/othertoken00/"},
        )
        assert second.status_code == 409
        assert "作品已存在" in second.text


def test_a_refused_duplicate_leaves_the_existing_candidate_untouched(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        _stub_gallery_fetch(client, monkeypatch)

        url = "https://exhentai.org/g/987654/appletoken99/"
        first = _post(client, "/manual-add", {"input": url})
        candidate_id = int(
            first.headers["location"].rstrip("/").rsplit("/", 1)[-1]
        )
        database = Database(settings.data_path / "ehbot.db")
        before = asyncio.run(database.get_candidate(candidate_id))
        _post(client, "/manual-add", {"input": url})
        after = asyncio.run(database.get_candidate(candidate_id))
        assert after is not None and before is not None
        assert after.status == before.status
        assert after.filter_reason == before.filter_reason


# ------------------------------------------------------------------ JSON API
# The mobile client cannot reach `/manual-add`: that route wants a session
# cookie plus a `csrf_token` form field and answers with a 302. `POST
# /api/v1/candidates/manual` runs the same shared ingest (`app.candidates.manual`),
# which is what these four cases pin: same acceptances, same refusals.


def _api_headers(client: TestClient) -> dict[str, str]:
    """The CSRF token the page layer embeds, as the browser would send it."""
    return {CSRF_HEADER: client.get("/manual-add").context["csrf_token"]}


def _manual_api(client: TestClient, url: str, headers: dict[str, str]):
    return client.post(
        "/api/v1/candidates/manual", json={"url": url}, headers=headers
    )


def test_api_manual_add_creates_the_candidate(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        fetched = _stub_gallery_fetch(client, monkeypatch, metadata={"Title": "B"})
        response = _manual_api(
            client,
            "https://exhentai.org/g/987654/appletoken99/",
            _api_headers(client),
        )

    assert response.status_code == 200
    assert response.json()["candidate_id"] >= 1
    # The gallery that was validated is the one that was fetched -- same
    # sequence as the page, not a shape check that skips the fetch.
    assert fetched == [(987654, "appletoken99")]


def test_api_manual_add_reports_the_existing_candidate_on_a_repeat(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    url = "https://exhentai.org/g/987654/appletoken99/"
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        _stub_gallery_fetch(client, monkeypatch)
        headers = _api_headers(client)
        first = _manual_api(client, url, headers)
        second = _manual_api(client, url, headers)

    assert first.status_code == 200
    # 409 rather than an error to fix: the client's move is to open the work,
    # and the id travels in `details` so it can do that without parsing text.
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "CANDIDATE_DUPLICATE"
    assert error["details"]["existing_candidate_id"] == (
        first.json()["candidate_id"]
    )


def test_api_manual_add_refuses_without_a_connection_and_creates_nothing(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        fetched = _stub_gallery_fetch(client, monkeypatch)
        response = _manual_api(
            client,
            "https://exhentai.org/g/987654/appletoken99/",
            _api_headers(client),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXHENTAI_NOT_CONFIGURED"
    # Refused before the link was looked at: no fetch, no row.
    assert fetched == []
    assert _no_candidate_count(settings) == 0


def test_api_manual_add_refuses_anything_that_is_not_a_gallery_link(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _configure_exhentai(client, monkeypatch)
        fetched = _stub_gallery_fetch(client, monkeypatch)
        response = _manual_api(
            client,
            "magnet:?xt=urn:btih:0123456789abcdef",
            _api_headers(client),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_LINK"
    assert fetched == []
    assert _no_candidate_count(settings) == 0
