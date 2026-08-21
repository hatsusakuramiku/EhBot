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


def test_readyz_reports_database_and_storage_are_ready(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert (settings.data_path / "ehbot.db").is_file()


def test_readyz_is_ready_without_a_configured_secret(tmp_path: Path) -> None:
    """A fresh deployment must come up without a hand-created secret file."""
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
        tag_translation_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert (
        settings.data_path / "private" / "session_secret_key"
    ).is_file()


def test_readyz_rejects_a_configured_secret_that_is_too_short(
    tmp_path: Path,
) -> None:
    """An explicit key is still validated; a truncated one is an operator error."""
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
        app_secret_key="too-short",
        tag_translation_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_sessions_survive_a_restart_without_a_configured_secret(
    tmp_path: Path,
) -> None:
    """The generated key is reused, otherwise every restart logs everyone out."""
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
        tag_translation_enabled=False,
    )

    with TestClient(create_app(settings)):
        pass
    stored = (
        settings.data_path / "private" / "session_secret_key"
    ).read_text(encoding="utf-8")

    with TestClient(create_app(settings)):
        pass
    reused = (
        settings.data_path / "private" / "session_secret_key"
    ).read_text(encoding="utf-8")

    assert stored == reused
    assert len(stored) >= 32


def test_readyz_detects_storage_becoming_unwritable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        settings.work_path.rmdir()
        settings.work_path.write_text("not a directory", encoding="utf-8")
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
