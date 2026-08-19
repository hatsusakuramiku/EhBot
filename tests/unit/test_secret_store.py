from pathlib import Path

from app.secrets import SecretStore


def test_secret_store_atomically_replaces_private_values(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "private")

    store.write("telegram_bot_token", "first-value")
    store.write("telegram_bot_token", "second-value")

    assert store.read("telegram_bot_token") == "second-value"
    assert store.is_configured("telegram_bot_token") is True
    assert [path.name for path in (tmp_path / "private").iterdir()] == [
        "telegram_bot_token"
    ]
