"""The seven settings tabs: rendering, redirects, saves, previews and dry runs.

Every tab at `/settings/{section}` produces a page, and every legacy path
redirects into its tab. Saves are tested through the same forms an operator would
use, so the binding between the template's field names and the handler's `Form()`
arguments is exercised rather than assumed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.api.status import SETTINGS_SECTIONS
from app.config import Settings
from app.conversion.naming import DEFAULT_LIBRARY_TEMPLATE
from app.db.database import Database
from app.main import create_app


def _settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
        torrent_enabled=False,
    )


def _authenticate(client: TestClient, settings: Settings) -> str:
    """Log in with the bootstrap password and return the csrf token."""
    pw = (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )
    login = client.get("/login")
    client.post(
        "/login",
        data={"password": pw, "csrf_token": login.context["csrf_token"]},
    )
    change = client.get("/settings/passwords")
    new = "new-password-with-12-characters"
    client.post(
        "/change-password",
        data={
            "current_password": pw,
            "new_password": new,
            "confirmation": new,
            "csrf_token": change.context["csrf_token"],
        },
    )
    return change.context["csrf_token"]


def _csrf(client: TestClient, section: str = "connections") -> str:
    """Fetch a csrf token from any settings tab."""
    return client.get(f"/settings/{section}").context["csrf_token"]


def _seed_candidate(database: Database, title: str = "A Title") -> int:
    """One candidate with a Title in metadata_values, for the dry-run scan.

    The title lives only in `metadata_values` -- `candidates` has no title
    column, because a title is a metadata field with a source and a confidence
    like any other.
    """
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (1, 'PENDING_REVIEW')"
        )
        connection.execute(
            "INSERT INTO metadata_values (candidate_id, field_name, field_value,"
            " value_source, confidence, is_manual) VALUES (1, 'Title', ?, 'EXHENTAI', 0.9, 0)",
            (title,),
        )
    return 1


# ---------------------------------------------------------------------------
#  Seven tabs
# ---------------------------------------------------------------------------


class TestSevenTabs:
    """Each section renders at its own URL, and an unknown section is a 404."""

    def test_every_section_renders(self, tmp_path: Path) -> None:
        """All seven tabs load without crashing."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            for code in SETTINGS_SECTIONS:
                page = client.get(f"/settings/{code}")
                assert page.status_code == 200, f"{code}"

    def test_every_section_has_its_label_in_the_title(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            for code in SETTINGS_SECTIONS:
                page = client.get(f"/settings/{code}")
                assert page.context["section"]["label"] in page.text

    def test_an_unknown_section_is_a_404(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            response = client.get("/settings/nonsense")
        assert response.status_code == 404

    def test_the_index_redirects_to_connections(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.get("/settings", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/settings/connections"


# ---------------------------------------------------------------------------
#  Legacy redirects
# ---------------------------------------------------------------------------


class TestLegacyRedirects:
    def test_connections_redirects(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            r = client.get("/connections", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/settings/connections"

    def test_sources_redirects(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            r = client.get("/sources", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/settings/sources"

    def test_auto_approval_rules_redirects(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            r = client.get("/auto-approval-rules", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/settings/auto-approval"

    def test_archive_settings_redirects(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            r = client.get("/archive-settings", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/settings/archive"

    def test_change_password_redirects(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            r = client.get("/change-password", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/settings/passwords"


# ---------------------------------------------------------------------------
#  Auth gate
# ---------------------------------------------------------------------------


class TestAuthGate:
    def test_an_unauthenticated_caller_is_sent_to_login(self, tmp_path: Path) -> None:
        with TestClient(create_app(_settings(tmp_path))) as client:
            response = client.get("/settings/connections", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_passwords_tab_is_accessible_with_bootstrap(self, tmp_path: Path) -> None:
        """The one tab the bounce must not bounce from."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            pw = (settings.data_path / "bootstrap_admin_password").read_text(
                encoding="utf-8"
            )
            login = client.get("/login")
            client.post(
                "/login",
                data={
                    "password": pw,
                    "csrf_token": login.context["csrf_token"],
                },
            )
            page = client.get("/settings/passwords")
        assert page.status_code == 200
        assert "管理员密码" in page.text


# ---------------------------------------------------------------------------
#  System tab
# ---------------------------------------------------------------------------


class TestSystemTab:
    def test_save_round_trips(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            saved = client.post(
                "/settings/system",
                data={
                    "csrf_token": csrf,
                    "source_concurrency": "5",
                    "poll_interval_ms": "4000",
                    "timezone": "Asia/Shanghai",
                },
                follow_redirects=False,
            )
            page = client.get("/settings/system")

        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings/system"
        assert page.context["system"]["source_concurrency"] == 5
        assert page.context["system"]["poll_interval_ms"] == 4000

    def test_out_of_bounds_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            response = client.post(
                "/settings/system",
                data={"csrf_token": csrf, "source_concurrency": "99"},
            )
        assert response.status_code == 400
        assert "并发上限" in response.text

    def test_an_empty_submission_restores_the_default(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/settings/system",
                data={"csrf_token": csrf, "poll_interval_ms": "10000"},
            )
            client.post(
                "/settings/system",
                data={"csrf_token": csrf, "poll_interval_ms": ""},
            )
            page = client.get("/settings/system")

        assert page.context["system"]["poll_interval_overridden"] is False

    def test_a_saved_timezone_reaches_every_page(self, tmp_path: Path) -> None:
        """The zone is published as a meta tag, which is how `ui.js` reads it.

        This is the only settings value that has to escape the settings page:
        `<time>` elements are rendered everywhere and localised in the browser,
        so a save that did not refresh `app.state.display_timezone` would leave
        every other page formatting in the old zone until a restart.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            before = client.get("/")
            client.post(
                "/settings/system",
                data={"csrf_token": csrf, "timezone": "Asia/Shanghai"},
            )
            after = client.get("/")

        assert 'name="display-timezone" content="UTC"' in before.text
        assert 'name="display-timezone" content="Asia/Shanghai"' in after.text


# ---------------------------------------------------------------------------
#  Paths tab — template preview
# ---------------------------------------------------------------------------


class TestPathsTab:
    def test_template_preview_shows_the_rendered_path(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/template/preview",
                data={
                    "csrf_token": csrf,
                    "library_template": "{category}/{artist}/{title}",
                },
            )
        assert page.status_code == 200
        assert page.context["template_preview"]["rendered"] == (
            "同人志/示例作者/示例标题 上 下卷.cbz"
        )

    def test_an_invalid_template_shows_an_error(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/template/preview",
                data={"csrf_token": csrf, "library_template": "../{title}"},
            )
        assert page.status_code == 400
        assert "不能包含" in page.text

    def test_a_valid_template_saves(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            saved = client.post(
                "/archive-settings/paths/template",
                data={
                    "csrf_token": csrf,
                    "library_template": "{category}/{title}",
                },
                follow_redirects=False,
            )
            page = client.get("/settings/paths")

        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings/paths"
        assert page.context["library_template"] == "{category}/{title}"

    def test_an_invalid_template_is_refused_at_save(self, tmp_path: Path) -> None:
        """Preview is a convenience; the save validates again on its own.

        Sent straight to the save endpoint without previewing first, which is
        the path an operator takes by pressing 保存 immediately -- so the refusal
        has to come from the handler, not from the preview it skipped.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            response = client.post(
                "/archive-settings/paths/template",
                data={"csrf_token": csrf, "library_template": "{category}/{oops}"},
            )
            page = client.get("/settings/paths")

        assert response.status_code == 400
        assert page.context["library_template"] == DEFAULT_LIBRARY_TEMPLATE

    def test_an_empty_template_restores_the_default(self, tmp_path: Path) -> None:
        """Clearing the field is 「恢复默认」, not 「把每本书放进库根目录」."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/archive-settings/paths/template",
                data={"csrf_token": csrf, "library_template": "{category}/{title}"},
            )
            cleared = client.post(
                "/archive-settings/paths/template",
                data={"csrf_token": csrf, "library_template": "  "},
                follow_redirects=False,
            )
            page = client.get("/settings/paths")

        assert cleared.status_code == 303
        assert page.context["library_template"] == DEFAULT_LIBRARY_TEMPLATE


# ---------------------------------------------------------------------------
#  Auto-approval tab — dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_reports_matches(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        _seed_candidate(database, "Matching Title")

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "condition_kind": ["condition"],
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["Matching Title"],
                },
            )
        assert page.status_code == 200
        assert page.context["dry_run"]["matched"] == 1
        assert page.context["dry_run"]["scanned"] >= 1

    def test_dry_run_with_no_match_returns_zero(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        _seed_candidate(database, "A Title")

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "condition_kind": ["condition"],
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["Does Not Exist"],
                },
            )
        assert page.status_code == 200
        assert page.context["dry_run"]["matched"] == 0

    def test_dry_run_with_bad_regex_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "condition_kind": ["regex"],
                    "condition_field": ["Title"],
                    "condition_operator": [""],
                    "condition_value": ["[unclosed"],
                },
            )
        assert page.status_code == 400

    def test_dry_run_with_two_conditions_matches_combined(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        _seed_candidate(database, "A Title")

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "group_operator": "AND",
                    "condition_kind": ["condition", "condition"],
                    "condition_field": ["Title", "Title"],
                    "condition_operator": ["=", "="],
                    "condition_value": ["A Title", "A Title"],
                },
            )
        assert page.status_code == 200
        assert page.context["dry_run"]["matched"] == 1

    def test_dry_run_with_divergent_conditions_matches_nothing(
        self, tmp_path: Path
    ) -> None:
        """AND group where one condition can never match is a hard zero."""
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        _seed_candidate(database, "A Title")

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "group_operator": "AND",
                    "condition_kind": ["condition", "condition"],
                    "condition_field": ["Title", "Title"],
                    "condition_operator": ["=", "="],
                    "condition_value": ["A Title", "Another"],
                },
            )
        assert page.status_code == 200
        assert page.context["dry_run"]["matched"] == 0

    def test_a_dry_run_approves_nothing(self, tmp_path: Path) -> None:
        """试跑不产生副作用 -- the candidate is in the same state afterwards.

        The rule under test is one that WOULD approve the seeded candidate, so a
        run that accidentally applied itself would be visible here rather than
        needing a rule crafted to miss.
        """
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        _seed_candidate(database, "A Title")

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "condition_kind": ["condition"],
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["A Title"],
                },
            )

        assert page.context["dry_run"]["matched"] == 1
        with sqlite3.connect(database.path) as connection:
            status = connection.execute(
                "SELECT status FROM candidates WHERE id = 1"
            ).fetchone()[0]
            actions = connection.execute(
                "SELECT COUNT(*) FROM review_actions"
            ).fetchone()[0]
            rules = connection.execute(
                "SELECT COUNT(*) FROM auto_approval_rules"
            ).fetchone()[0]

        assert status == "PENDING_REVIEW"
        assert actions == 0
        # A trial run is not a save either: the rule tried here was never stored.
        assert rules == 0


class TestRuleSaving:
    """`validate_rule_ast` is the gate, whatever the browser thought."""

    def test_a_rule_saves_and_appears_on_the_tab(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            saved = client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "Only Doujinshi",
                    "priority": "50",
                    "enabled": "on",
                    "condition_kind": ["condition"],
                    "condition_field": ["Category"],
                    "condition_operator": ["="],
                    "condition_value": ["同人志"],
                },
                follow_redirects=False,
            )
            page = client.get("/settings/auto-approval")

        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings/auto-approval"
        assert "Only Doujinshi" in page.text

    def test_an_invalid_regex_is_refused_at_save(self, tmp_path: Path) -> None:
        """The acceptance criterion: 规则非法正则在保存时拒绝, and nothing is stored.

        `settings.js` compiles the pattern in the browser too, but that check is
        a courtesy -- this posts straight past it, which is what a script-off
        browser and a curl call both do.
        """
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            response = client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "Broken",
                    "priority": "50",
                    "condition_kind": ["regex"],
                    "condition_field": ["Title"],
                    "condition_operator": [""],
                    "condition_value": ["[unclosed"],
                },
            )

        assert response.status_code == 400
        with sqlite3.connect(database.path) as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM auto_approval_rules"
            ).fetchone()[0]
        assert stored == 0

    def test_a_rule_without_a_name_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            response = client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "  ",
                    "priority": "50",
                    "condition_kind": ["condition"],
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["A Title"],
                },
            )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
#  JSON API parity
# ---------------------------------------------------------------------------


class TestApiParity:
    """The page and the JSON endpoint read the same snapshot."""

    def test_settings_section_has_the_same_keys_as_the_json_endpoint(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            for code in SETTINGS_SECTIONS:
                page = client.get(f"/settings/{code}")
                api = client.get(f"/api/v1/settings/{code}")
                # The page has extra keys (csrf_token, section, tabs, error,
                # notice) that the API does not, and the API is the authority
                # on what the section contains. Every key in the api response
                # must also be in the page context.
                page_keys = set(page.context)
                api_keys = set(api.json())
                assert page_keys.issuperset(api_keys), (
                    f"{code}: page missing {api_keys - page_keys}"
                )