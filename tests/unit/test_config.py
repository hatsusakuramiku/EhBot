from pathlib import Path

import pytest

from app.config import Settings


def test_settings_reads_secrets_from_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "app_secret_key"
    secret_file.write_text("file-secret-value\n", encoding="utf-8")
    monkeypatch.setenv("APP_SECRET_KEY_FILE", str(secret_file))

    settings = Settings.from_env()

    assert settings.app_secret_key == "file-secret-value"


def test_settings_rejects_ambiguous_secret_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "app_secret_key"
    secret_file.write_text("file-secret-value", encoding="utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "direct-secret-value")
    monkeypatch.setenv("APP_SECRET_KEY_FILE", str(secret_file))

    with pytest.raises(ValueError, match="either APP_SECRET_KEY or APP_SECRET_KEY_FILE"):
        Settings.from_env()
