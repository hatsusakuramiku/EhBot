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


def test_readyz_reports_database_and_storage_are_ready(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert (settings.data_path / "ehbot.db").is_file()


def test_readyz_rejects_missing_authentication_secrets(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
