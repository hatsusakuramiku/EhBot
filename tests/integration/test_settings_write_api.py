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


PASSWORDS = "/api/v1/settings/passwords"


def _put_passwords(client: TestClient, csrf: str, body: dict):
    return client.put(PASSWORDS, json=body, headers={CSRF_HEADER: csrf})


def test_an_archive_password_is_added_and_never_read_back(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put_passwords(
            client,
            csrf,
            {"action": "add", "name": "常用", "password": "s3cret", "priority": 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert [entry["name"] for entry in payload["passwords"]] == ["常用"]
    # The whole point of the store: the value is not in the response, only the
    # fact that an entry exists.
    assert "s3cret" not in json.dumps(payload)


def test_a_deleted_password_is_gone_from_the_section(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        added = _put_passwords(
            client, csrf, {"action": "add", "name": "常用", "password": "s3cret"}
        )
        password_id = added.json()["passwords"][0]["password_id"]
        response = _put_passwords(
            client, csrf, {"action": "delete", "password_id": password_id}
        )

    assert response.status_code == 200
    assert response.json()["passwords"] == []


def test_a_password_id_is_required_to_delete(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put_passwords(client, csrf, {"action": "delete"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PASSWORD_ID_REQUIRED"


def test_generating_the_api_key_returns_the_plaintext_exactly_once(
    tmp_path: Path,
) -> None:
    """The one action whose response is allowed to carry a secret.

    It exists nowhere else once this response is gone, so a client that misses it
    has to mint a replacement -- which is why it is returned here rather than
    stashed for a later read, the way the page's one-shot reveal does.
    """
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        generated = _put_passwords(client, csrf, {"action": "api-key-generate"})
        plaintext = generated.json()["api_key_plaintext"]
        later = client.get("/api/v1/settings/passwords").json()

    assert generated.status_code == 200
    assert plaintext.startswith("ehk_")
    assert generated.json()["api_key"]["configured"] is True
    # A later read reports the key, never its plaintext.
    assert later["api_key"]["configured"] is True
    assert "api_key_plaintext" not in later
    assert plaintext not in json.dumps(later)


def test_revoking_the_api_key_leaves_the_section_unconfigured(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        _put_passwords(client, csrf, {"action": "api-key-generate"})
        response = _put_passwords(client, csrf, {"action": "api-key-revoke"})

    assert response.status_code == 200
    assert response.json()["api_key"]["configured"] is False


def test_an_unknown_password_action_lists_the_allowed_ones(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = _put_passwords(client, csrf, {"action": "explode"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "ACTION_UNKNOWN"
    assert set(error["details"]["allowed"]) == {
        "add",
        "delete",
        "api-key-generate",
        "api-key-revoke",
    }


# ---------------------------------------------------------------- R74 来源

SOURCES = "/api/v1/settings/sources"


def _channel(csrf: str, **overrides) -> dict:
    body = {
        "source_type": "CHANNEL",
        "chat_id": -1001234567890,
        "display_name": "某个频道",
        "enabled": False,
    }
    body.update(overrides)
    return body


def test_a_source_is_created_and_read_back(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.post(
            SOURCES, json=_channel(csrf), headers={CSRF_HEADER: csrf}
        )
        snapshot = client.get(SOURCES).json()

    assert response.status_code == 200
    rows = snapshot["sources"]
    assert len(rows) == 1
    assert rows[0]["chat_id"] == -1001234567890
    assert rows[0]["display_name"] == "某个频道"
    # Rows are created with whatever the operator asked for, including the flag:
    # a source added and left disabled is the normal first step.
    assert rows[0]["enabled"] is False


def test_a_save_upserts_on_the_chat_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.post(SOURCES, json=_channel(csrf), headers={CSRF_HEADER: csrf})
        client.post(
            SOURCES,
            json=_channel(csrf, display_name="改过的名字", enabled=True),
            headers={CSRF_HEADER: csrf},
        )
        rows = client.get(SOURCES).json()["sources"]

    assert len(rows) == 1
    assert rows[0]["display_name"] == "改过的名字"
    assert rows[0]["enabled"] is True


def test_the_chat_id_sign_has_to_match_the_type(tmp_path: Path) -> None:
    """A channel's id is negative and a private chat's is positive.

    The sign *is* the kind in Telegram, so a submission that disagrees is the
    operator on the wrong row of the type select, and it is refused before the
    insert rather than by a constraint.
    """
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.post(
            SOURCES,
            json=_channel(csrf, chat_id=12345),
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SOURCE_INVALID"


def test_bad_rules_are_refused_with_the_pages_sentence(tmp_path: Path) -> None:
    """A negative size fails the same guard the page applies first.

    `parse_source` checks identity and the size together -- the page has done
    that since before this endpoint existed -- so the sentence is the combined
    one rather than a size-specific one.
    """
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.post(
            SOURCES,
            json=_channel(csrf, max_attachment_size_mb=-1),
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "SOURCE_INVALID"
    assert error["message"] == "来源类型、ID、名称或附件上限无效"


def test_a_batch_apply_reports_its_own_rule_error(tmp_path: Path) -> None:
    """The batch reader checks the rules alone, so its sentence is the rule's."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.post(SOURCES, json=_channel(csrf), headers={CSRF_HEADER: csrf})
        source_id = client.get(SOURCES).json()["sources"][0]["source_id"]
        response = client.put(
            f"{SOURCES}/batch",
            json={
                "action": "apply",
                "source_ids": [source_id],
                "min_rating": "很高",
            },
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "RATING_INVALID"
    assert error["message"] == "最低评分格式无效"


def test_tags_are_cleaned_the_same_way_the_form_cleans_them(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.post(
            SOURCES,
            json=_channel(
                csrf,
                required_tags="NTR, 巨乳",
                allowed_archive_formats=["zip", "rar", "nope"],
            ),
            headers={CSRF_HEADER: csrf},
        )
        row = client.get(SOURCES).json()["sources"][0]

    assert row["required_tags"] == ["ntr", "巨乳"]
    # An unlisted format is dropped rather than stored: the vocabulary is the
    # deployment's, not the caller's.
    assert row["allowed_archive_formats"] == ["zip", "rar"]


def test_the_batch_flips_the_flag(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.post(SOURCES, json=_channel(csrf), headers={CSRF_HEADER: csrf})
        source_id = client.get(SOURCES).json()["sources"][0]["source_id"]
        enabled = client.put(
            f"{SOURCES}/batch",
            json={"action": "enable", "source_ids": [source_id]},
            headers={CSRF_HEADER: csrf},
        ).json()["sources"][0]["enabled"]
        disabled = client.put(
            f"{SOURCES}/batch",
            json={"action": "disable", "source_ids": [source_id]},
            headers={CSRF_HEADER: csrf},
        ).json()["sources"][0]["enabled"]

    assert enabled is True
    assert disabled is False


def test_the_batch_applies_a_rule_set(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.post(SOURCES, json=_channel(csrf), headers={CSRF_HEADER: csrf})
        source_id = client.get(SOURCES).json()["sources"][0]["source_id"]
        row = client.put(
            f"{SOURCES}/batch",
            json={
                "action": "apply",
                "source_ids": [source_id],
                "required_tags": "ntr",
                "min_rating": "4.5",
            },
            headers={CSRF_HEADER: csrf},
        ).json()["sources"][0]

    assert row["required_tags"] == ["ntr"]
    assert row["min_rating"] == 4.5


def test_the_batch_deletes_by_tombstone(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.post(SOURCES, json=_channel(csrf), headers={CSRF_HEADER: csrf})
        source_id = client.get(SOURCES).json()["sources"][0]["source_id"]
        client.put(
            f"{SOURCES}/batch",
            json={"action": "delete", "source_ids": [source_id]},
            headers={CSRF_HEADER: csrf},
        )
        rows = client.get(SOURCES).json()["sources"]

    # A tombstone, not a DELETE: the ingester has to know the chat was dropped
    # rather than never seen.
    assert rows == []


def test_an_unknown_batch_action_lists_the_allowed_ones(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            f"{SOURCES}/batch",
            json={"action": "explode", "source_ids": [1]},
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "ACTION_UNKNOWN"
    assert error["details"]["allowed"] == ["apply", "delete", "disable", "enable"]


def test_a_batch_without_a_selection_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            f"{SOURCES}/batch",
            json={"action": "enable", "source_ids": []},
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SOURCE_IDS_REQUIRED"


def test_the_write_needs_the_same_gates_as_any_other(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.post(SOURCES, json=_channel(csrf))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


# ---------------------------------------------------------------- R75 解析规则

PARSE = "/api/v1/settings/parse"


def test_the_parse_scheme_is_stored_and_read_back(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            PARSE,
            json={
                "require_gallery_link": True,
                "accept_photo": True,
                "accept_archive": True,
                # `accept_preview` absent: an unchecked box sends nothing, so
                # absence has to mean off rather than the shipped default.
                "archive_formats": ["zip", "cbz"],
            },
            headers={CSRF_HEADER: csrf},
        )
        rules = client.get(PARSE).json()["parse_rules"]

    assert response.status_code == 200
    assert rules["require_gallery_link"] is True
    assert rules["accept_photo"] is True
    assert rules["accept_archive"] is True
    assert rules["accept_preview"] is False
    assert rules["archive_formats"] == ["zip", "cbz"]


def test_an_unlisted_format_is_dropped_not_stored(tmp_path: Path) -> None:
    """The vocabulary is the parser's; a typo must not become a rule."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        client.put(
            PARSE,
            json={"accept_archive": True, "archive_formats": ["zip", "tar"]},
            headers={CSRF_HEADER: csrf},
        )
        rules = client.get(PARSE).json()["parse_rules"]

    assert rules["archive_formats"] == ["zip"]


def test_reset_puts_the_shipped_scheme_back(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        shipped = client.get(PARSE).json()["parse_defaults"]
        client.put(
            PARSE,
            json={"accept_photo": True, "archive_formats": ["rar"]},
            headers={CSRF_HEADER: csrf},
        )
        response = client.post(
            PARSE + "/reset", json={}, headers={CSRF_HEADER: csrf}
        )
        rules = response.json()["parse_rules"]

    assert response.status_code == 200
    assert rules["accept_photo"] == shipped["accept_photo"]
    assert rules["archive_formats"] == shipped["archive_formats"]


def test_the_default_prompt_is_stored_as_use_the_default(
    tmp_path: Path,
) -> None:
    """Otherwise every save would pin the shipped text as an override."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        default = client.get(PARSE).json()["candidate_prompt_default"]
        shipped = client.put(
            PARSE + "/ai",
            json={"ai_candidate_prompt": default, "ai_candidate_fallback": "reject"},
            headers={CSRF_HEADER: csrf},
        ).json()["candidate_admission"]
        custom = client.put(
            PARSE + "/ai",
            json={"ai_candidate_prompt": "只看有压缩包的", "ai_candidate_fallback": "reject"},
            headers={CSRF_HEADER: csrf},
        ).json()["candidate_admission"]

    # The snapshot reads the prompt back resolved: an empty stored override says
    # 「use the shipped text」, and the flag is what tells the two apart.
    assert shipped["prompt"] == default
    assert shipped["prompt_overridden"] is False
    assert custom["prompt"] == "只看有压缩包的"
    assert custom["prompt_overridden"] is True


def test_the_gate_switches_travel_with_the_prompt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        admission = client.put(
            PARSE + "/ai",
            json={
                "ai_candidate_enabled": True,
                "ai_candidate_override_parse_rules": False,
                "ai_candidate_fallback": "accept",
            },
            headers={CSRF_HEADER: csrf},
        ).json()["candidate_admission"]

    assert admission["enabled"] is True
    assert admission["override_parse_rules"] is False
    assert admission["fallback"] == "accept"


def test_an_unknown_fallback_is_refused_by_the_service(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(
            PARSE + "/ai",
            json={"ai_candidate_fallback": "explode"},
            headers={CSRF_HEADER: csrf},
        )

    assert response.status_code == 400
    assert (
        response.json()["error"]["code"] == "AI_CANDIDATE_FALLBACK_INVALID"
    )


def test_the_parse_writes_need_the_same_gates_as_any_other(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _authenticate(client, settings)
        response = client.put(PARSE, json={"accept_photo": True})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"
