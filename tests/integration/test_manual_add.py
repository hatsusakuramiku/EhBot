import asyncio

from fastapi.testclient import TestClient

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


def test_manual_add_page_renders(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        page = client.get("/manual-add")
        assert page.status_code == 200
        assert "手动添加任务" in page.text


def test_manual_add_rejects_unrecognised_input(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        response = _post(client, "/manual-add", {"input": "https://example.com/thing"})
        assert response.status_code == 400
        assert "无法识别链接" in response.text


def test_manual_magnet_without_qbittorrent_returns_clear_error(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
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
        assert "qBittorrent" in response.text


def test_manual_eh_link_creates_approved_candidate(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)

        async def _noop_fetch(candidate_id: int) -> dict:
            return {}

        # The real app wiring creates an ExHentai service; stub its network
        # metadata call so the test needs no gallery HTTP round-trip.
        monkeypatch.setattr(
            client.app.state.exhentai_service,
            "fetch_metadata_for_candidate",
            _noop_fetch,
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
        candidate_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])

        database = Database(settings.data_path / "ehbot.db")
        candidate = asyncio.run(database.get_candidate(candidate_id))
        assert candidate is not None
        assert candidate.status == "APPROVED"
        assert candidate.ex_gid == 987654
        assert candidate.ex_gallery_token == "appletoken99"


def _stub_torrents(client: TestClient, monkeypatch) -> list[int]:
    """Make the app think qBittorrent is configured, without a server.

    The magnet path checks that prerequisite before it touches the database, so
    a dedup test has to get past it; the enqueue is stubbed for the same reason
    the gallery test stubs metadata -- no network in a unit of logic.
    """
    from types import SimpleNamespace

    service = client.app.state.archive_settings_service

    async def fake_torrent_client():
        return SimpleNamespace(is_configured=True)

    async def fake_torrent_client_view():
        return {"configured": True}

    enqueued: list[int] = []

    async def record_enqueue(candidate_id: int) -> None:
        enqueued.append(candidate_id)

    monkeypatch.setattr(service, "torrent_client", fake_torrent_client)
    monkeypatch.setattr(service, "torrent_client_view", fake_torrent_client_view)
    monkeypatch.setattr(
        client.app.state.download_service,
        "enqueue_torrent_download",
        record_enqueue,
    )
    return enqueued


def _noop_metadata(monkeypatch, client: TestClient) -> None:
    async def _noop_fetch(candidate_id: int) -> dict:
        return {}

    monkeypatch.setattr(
        client.app.state.exhentai_service,
        "fetch_metadata_for_candidate",
        _noop_fetch,
    )


def test_pasting_the_same_gallery_twice_is_refused_with_a_link(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _noop_metadata(monkeypatch, client)

        url = "https://exhentai.org/g/987654/appletoken99/"
        first = _post(client, "/manual-add", {"input": url})
        assert first.status_code == 303
        candidate_id = int(
            first.headers["location"].rstrip("/").rsplit("/", 1)[-1]
        )

        second = _post(client, "/manual-add", {"input": url})
        assert second.status_code == 409
        assert "该画廊已有候选" in second.text
        assert f"查看已有候选 #{candidate_id}" in second.text
        assert f"/works/{candidate_id}" in second.text
        # It stayed on the form rather than redirecting: the paste doing
        # nothing is what the operator has to see.
        assert second.headers.get("location") is None

        import sqlite3

        with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM candidates"
            ).fetchone()[0]
        assert rows == 1


def test_a_different_token_for_the_same_gallery_is_still_a_duplicate(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _noop_metadata(monkeypatch, client)

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
        assert "该画廊已有候选" in second.text


def test_pasting_the_same_magnet_twice_is_refused(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        enqueued = _stub_torrents(client, monkeypatch)

        magnet = (
            "magnet:?xt=urn:btih:"
            "0123456789abcdef0123456789abcdef01234567&dn=test"
        )
        first = _post(client, "/manual-add", {"input": magnet})
        assert first.status_code == 303

        second = _post(client, "/manual-add", {"input": magnet})
        assert second.status_code == 409
        assert "该磁力链接已有候选" in second.text

        import sqlite3

        with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM candidates"
            ).fetchone()[0]
        assert rows == 1
        # Exactly one enqueue: the duplicate never reached the queue.
        assert len(enqueued) == 1


def test_a_preview_link_is_accepted_by_manual_add(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _noop_metadata(monkeypatch, client)

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


def test_a_refused_duplicate_leaves_the_existing_candidate_untouched(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        _noop_metadata(monkeypatch, client)

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
