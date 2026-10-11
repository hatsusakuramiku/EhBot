"""The settings tabs over JSON: `PUT /api/v1/settings/{connections,system}`.

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
SYSTEM = "/api/v1/settings/system"


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


def test_a_cadence_is_stored_and_read_back(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            SYSTEM,
            json={"poll_interval_ms": "5000"},
            headers={CSRF_HEADER: csrf},
        )
        read_back = client.get("/api/v1/settings/system").json()

    assert response.status_code == 200
    assert response.json()["system"]["poll_interval_ms"] == 5000
    # Stored, not merely echoed: a second read agrees.
    assert read_back["system"]["poll_interval_ms"] == 5000


def test_only_the_fields_that_were_sent_are_touched(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        headers = {CSRF_HEADER: csrf}
        client.put(SYSTEM, json={"poll_interval_ms": "5000"}, headers=headers)
        client.put(SYSTEM, json={"source_concurrency": "2"}, headers=headers)
        snapshot = client.get("/api/v1/settings/system").json()["system"]

    # The second write must not have reset the first: an absent field is left
    # alone, which is what the form does and what a partial client needs.
    assert snapshot["poll_interval_ms"] == 5000
    assert snapshot["source_concurrency"] == 2


def test_an_out_of_range_cadence_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            SYSTEM,
            json={"poll_interval_ms": "1"},
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    error = response.json()["error"]
    # The envelope is this layer's contract; which code the service picks for
    # which bound is its own business and is covered by its unit tests.
    assert error["code"]
    assert "必须在" in error["message"]


def test_an_empty_body_says_what_could_have_been_sent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(SYSTEM, json={}, headers={CSRF_HEADER: csrf})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "SETTINGS_REQUIRED"
    assert "poll_interval_ms" in error["details"]["allowed"]


def test_saving_the_timezone_recaches_it_on_app_state(tmp_path: Path) -> None:
    """The hook that a JSON-only save would otherwise skip.

    The shell renders timestamps from `app.state.display_timezone` and
    `route_source` reads `app.state.download_source_priority`; both are
    synchronous readers. A write that stored the value without re-caching it
    would look like it did nothing until a restart, so this asserts the cache,
    not the row.
    """
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            SYSTEM,
            json={"timezone": "Asia/Tokyo"},
            headers={CSRF_HEADER: csrf},
        )
        cached = client.app.state.display_timezone

    assert response.status_code == 200
    assert cached == "Asia/Tokyo"
