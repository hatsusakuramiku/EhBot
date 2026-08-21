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


def test_settings_loads_explicit_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.1, 10.0.0.2")
    monkeypatch.setenv("APP_ROOT_PATH", "/ehbot")

    settings = Settings.from_env()

    assert settings.trust_proxy_headers is True
    assert settings.trusted_proxy_ips == ("10.0.0.1", "10.0.0.2")
    assert settings.app_root_path == "/ehbot"

def test_torrent_settings_fall_back_to_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator typo in a bound must not take the service down."""
    monkeypatch.setenv("TORRENT_POLL_SECONDS", "not-a-number")
    monkeypatch.setenv("TORRENT_CATEGORY", "   ")
    monkeypatch.setenv("TORRENT_ENABLED", "false")
    monkeypatch.setenv("TORRENT_KEEP_SEEDING", "false")

    settings = Settings.from_env()

    assert settings.torrent_poll_seconds == 15
    assert settings.torrent_category == "ehbot"
    assert settings.torrent_enabled is False
    # Not defaulted: dropping a seed is only ever done when asked.
    assert settings.torrent_keep_seeding is False
