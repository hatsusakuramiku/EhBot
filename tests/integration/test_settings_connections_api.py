"""The 连接 settings tab over JSON: `PUT /api/v1/settings/connections` (R67).

The mobile client cannot reach `/connections/*` -- those routes want a session
cookie plus a `csrf_token` form field and answer with a 302. This is the JSON
surface for the same `ConnectionManager` calls, so what these cases pin is the
JSON layer's own contract: its gates, its refusals, and the fact that a
credential never comes back in the response.

The manager calls themselves are the page's, and their behaviour is covered by
`test_settings_web.py`; repeating those assertions here would be a second copy of
the same coverage rather than more of it.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.main import create_app

from tests.integration.test_settings_web import _authenticate, _settings

WRITE = "/api/v1/settings/connections"


def _put(client: TestClient, csrf: str, body: dict):
    return client.put(WRITE, json=body, headers={CSRF_HEADER: csrf})


def test_an_unknown_action_lists_the_allowed_ones(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put(client, csrf, {"action": "explode"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "ACTION_UNKNOWN"
    assert "telegram" in error["details"]["allowed"]
    assert "exhentai" in error["details"]["allowed"]


def test_a_missing_credential_names_the_field(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put(client, csrf, {"action": "telegram"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BOT_TOKEN_REQUIRED"
    # Named with the field, so the client can point at the input.
    assert error["details"]["field"] == "bot_token"


def test_the_login_code_and_the_2fa_password_are_one_of_the_two(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put(client, csrf, {"action": "telegram-user-verify"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CODE_OR_PASSWORD_REQUIRED"


def test_disconnecting_returns_the_snapshot_and_never_a_credential(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put(client, csrf, {"action": "telegram-disconnect"})

    assert response.status_code == 200
    payload = response.json()
    # The shape the GET already serves, plus the tab strip.
    assert payload["section"]["code"] == "connections"
    assert "connections" in payload
    # A credential is reported as a state, never as a value: the response is the
    # snapshot, so no field could carry the token back even if one were stored.
    assert "bot_token" not in json.dumps(payload)


def test_the_write_gate_matches_every_other_action(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        response = client.put(WRITE, json={"action": "telegram-disconnect"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"
