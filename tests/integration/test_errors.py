from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import AppError
from app.main import create_app


def test_application_errors_have_a_stable_public_response(tmp_path: Path) -> None:
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
    )
    app = create_app(settings)

    @app.get("/test-error")
    async def raise_test_error() -> None:
        raise AppError("TEST_ERROR", "Visible message", status_code=409)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "TEST_ERROR", "message": "Visible message"}
    }
