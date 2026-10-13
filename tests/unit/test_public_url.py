import pytest

from app.config import Settings
from app.public_url import normalize_origin, PublicURLMiddleware


@pytest.mark.parametrize("value,expected", [
    ("https://EHBot.example:443/", "https://ehbot.example"),
    ("http://localhost:80", "http://localhost"),
    ("https://ehbot.example:8443", "https://ehbot.example:8443"),
    ("http://[::1]:8080", "http://[::1]:8080"),
])
def test_normalized_public_origin(value, expected):
    assert Settings(public_base_url=value).public_base_url == expected


@pytest.mark.parametrize("value", [
    "ehbot.example", "wss://ehbot.example", "https://user:secret@ehbot.example",
    "https://ehbot.example/path", "https://ehbot.example?", "https://ehbot.example#",
    "https://ehbot.example:0", "https://ehbot.example:65536", "https://ehbot.example:",
    "https://*.example", "https://bad_host", "https://ehbot.example\n", "https://ehbot.example\\path",
])
def test_invalid_public_origin_fails_configuration(value):
    with pytest.raises(ValueError):
        Settings(public_base_url=value)


def test_public_url_from_env(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://EHBot.example/")
    assert Settings.from_env().public_base_url == "https://ehbot.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,scheme", [("http", "https"), ("websocket", "wss")])
async def test_scope_preserves_actual_client_and_prefix(kind, scheme):
    captured = []
    async def app(scope, receive, send):
        captured.append(scope)
    scope = {"type": kind, "scheme": "http", "client": ("172.18.0.1", 4321),
             "path": "/ehbot/ws/ui", "root_path": "/ehbot", "headers": [(b"host", b"internal"),
             (b"x-forwarded-for", b"203.0.113.4")]}
    await PublicURLMiddleware(app, normalize_origin("https://ehbot.example"))(scope, None, None)
    assert captured[0]["scheme"] == scheme
    assert captured[0]["client"] == scope["client"]
    assert captured[0]["path"] == scope["path"]
    assert captured[0]["root_path"] == scope["root_path"]
    assert dict(captured[0]["headers"])[b"host"] == b"ehbot.example"
    assert dict(scope["headers"])[b"host"] == b"internal"
