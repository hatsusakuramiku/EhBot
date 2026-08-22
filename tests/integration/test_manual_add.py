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
    change = client.get("/change-password")
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