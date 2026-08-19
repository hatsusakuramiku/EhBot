from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_healthz_reports_process_is_alive(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
