"""The seven settings tabs: rendering, redirects, saves, previews and dry runs.

Every tab at `/settings/{section}` produces a page, and every legacy path
redirects into its tab. Saves are tested through the same forms an operator would
use, so the binding between the template's field names and the handler's `Form()`
arguments is exercised rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path

import httpx

from fastapi.testclient import TestClient
import pytest

from app.api.status import SETTINGS_SECTIONS
from app.config import Settings
from app.conversion.naming import DEFAULT_LIBRARY_TEMPLATE
from app.db.database import Database
from app.main import create_app

from tests.integration.markup import nested_form_lines


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
                    "log_level": "DEBUG",
                },
                follow_redirects=False,
            )
            page = client.get("/settings/system")

        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings/system"
        assert page.context["system"]["source_concurrency"] == 5
        assert page.context["system"]["poll_interval_ms"] == 4000
        assert page.context["system"]["log_level"] == "DEBUG"
        assert page.context["system"]["log_access"] is True
        assert page.context["logs"]["configured_level"] == "DEBUG"
        assert page.context["logs"]["access_log"] is True
        assert 'option value="DEBUG" selected' in page.text

    def test_info_closes_access_logging_immediately(self, tmp_path: Path) -> None:
        from app.logging import DropAllFilter

        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/settings/system",
                data={"csrf_token": csrf, "log_level": "DEBUG"},
            )
            assert not any(
                isinstance(item, DropAllFilter)
                for item in logging.getLogger("uvicorn.access").filters
            )

            client.post(
                "/settings/system",
                data={"csrf_token": csrf, "log_level": "INFO"},
            )
            page = client.get("/settings/system")

        assert page.context["system"]["log_level"] == "INFO"
        assert page.context["logs"]["access_log"] is False
        assert any(
            isinstance(item, DropAllFilter)
            for item in logging.getLogger("uvicorn.access").filters
        )

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


class TestLogTail:
    """The in-app log viewer, from the file on disk to the rendered tab.

    Covered end to end rather than at `read_log_tail` alone because the defect
    this guards against lived between the layers: the formatter dropped
    `error_message`, so a failed pack reached the page with a bare error code
    and no cause. Every layer had a passing test and the operator still could
    not read the failure.
    """

    @staticmethod
    def _write_log(settings: Settings, payloads: list[dict]) -> None:
        import json

        settings.log_dir.mkdir(parents=True, exist_ok=True)
        (settings.log_dir / "ehbot.log").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in payloads)
            + "\n",
            encoding="utf-8",
        )

    def test_a_failure_shows_its_message_and_not_only_its_code(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        self._write_log(
            settings,
            [
                {
                    "timestamp": "2026-09-03T01:00:00+00:00",
                    "level": "WARNING",
                    "logger": "app.conversion.service",
                    "event": "conversion_job_failed",
                    "job_id": 7,
                    "candidate_id": 42,
                    "error_code": "ARCHIVE_CORRUPT",
                    "error_message": "分卷 2 缺失，压缩包无法解开",
                }
            ],
        )
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/settings/system")
            payload = client.get("/api/v1/settings/system").json()

        entry = page.context["logs"]["entries"][0]
        assert entry["error_code"] == "ARCHIVE_CORRUPT"
        assert entry["error_message"] == "分卷 2 缺失，压缩包无法解开"
        # The page renders it, so an operator does not have to open the JSON.
        assert "分卷 2 缺失" in page.text
        # And the JSON body cannot disagree with the page -- the rule every
        # other section follows.
        assert payload["logs"]["entries"][0] == entry

    def test_a_line_without_a_message_renders_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Most lines have no `error_message`; they must not gain empty markup."""
        settings = _settings(tmp_path)
        self._write_log(
            settings,
            [
                {
                    "timestamp": "2026-09-03T01:00:00+00:00",
                    "level": "INFO",
                    "logger": "app.downloads.service",
                    "event": "download_job_completed",
                    "job_id": 3,
                }
            ],
        )
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/settings/system")

        assert page.context["logs"]["entries"][0]["error_message"] is None
        assert "ui-log-message" not in page.text


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
            "同人志/示例作者/サンプル作品.cbz"
        )

    def test_template_preview_follows_the_submitted_title_source(
        self, tmp_path: Path
    ) -> None:
        """The radio is honoured before it is saved.

        Previewing exists to answer 「这个模板会产生什么路径」, and the answer
        depends on which title `{title}` resolves to. Reading the stored value
        here would show the path of the preference the operator is abandoning.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/template/preview",
                data={
                    "csrf_token": csrf,
                    "library_template": "{category}/{artist}/{title}",
                    "title_source": "english",
                },
            )
        assert page.status_code == 200
        # The colon and the slash are why 日文标题 is the default.
        assert page.context["template_preview"]["rendered"] == (
            "同人志/示例作者/Sample Work Vol.1 2.cbz"
        )

    def test_the_title_source_saves_with_the_template(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            saved = client.post(
                "/archive-settings/paths/template",
                data={
                    "csrf_token": csrf,
                    "library_template": "{artist}/{title}",
                    "title_source": "english",
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303
            page = client.get("/settings/paths")
        assert page.context["title_source"] == "english"

    def test_a_template_may_name_one_language_directly(self, tmp_path: Path) -> None:
        """`{japanese_title}` satisfies the title requirement on its own.

        An operator who wants both languages in the path needs the template to
        accept a language-specific placeholder without also carrying the
        preference-following `{title}`.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/template/preview",
                data={
                    "csrf_token": csrf,
                    "library_template": "{japanese_title} [{english_title}]",
                },
            )
        assert page.status_code == 200
        assert page.context["template_preview"]["rendered"] == (
            "サンプル作品 [Sample Work Vol.1 2].cbz"
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
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["Does Not Exist"],
                },
            )
        assert page.status_code == 200
        assert page.context["dry_run"]["matched"] == 0

    def test_dry_run_refuses_a_comparison_a_field_cannot_make(
        self, tmp_path: Path
    ) -> None:
        """Text fields cannot range-compare, and the dry run refuses before trying."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/auto-approval-rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "condition_field": ["Title"],
                    "condition_operator": [">"],
                    "condition_value": ["100"],
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

    def test_a_rule_can_be_edited_in_place(self, tmp_path: Path) -> None:
        """编辑 loads the stored rule and 保存 overwrites it.

        The reported problem was that a saved rule could only be enabled or
        disabled: the editor always posted `rule_id=None`, so every save was an
        insert and the only way to fix a wrong rule was to add a second one
        beside it.
        """
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "Only Doujinshi",
                    "priority": "50",
                    "enabled": "on",
                    "condition_field": ["Category"],
                    "condition_operator": ["="],
                    "condition_value": ["同人志"],
                },
                follow_redirects=False,
            )
            rules = asyncio.run(database.list_auto_approval_rules())
            rule_id = rules[0].rule_id

            editing = client.get(f"/auto-approval-rules/{rule_id}/edit")
            assert editing.status_code == 200
            assert editing.context["edit_rule"]["name"] == "Only Doujinshi"
            assert editing.context["edit_rule"]["rows"][0]["value"] == "同人志"

            saved = client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": editing.context["csrf_token"],
                    "rule_id": str(rule_id),
                    "name": "Only Manga",
                    "priority": "10",
                    "enabled": "on",
                    "condition_field": ["Category"],
                    "condition_operator": ["="],
                    "condition_value": ["漫画"],
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303

        rules = asyncio.run(database.list_auto_approval_rules())
        # Overwritten, not duplicated: that is the whole difference from before.
        assert len(rules) == 1
        assert rules[0].rule_id == rule_id
        assert rules[0].name == "Only Manga"
        assert rules[0].priority == 10
        # One row stores as that row's node rather than a group of one, which is
        # what `_parse_rule_condition` does on a create too.
        assert rules[0].condition["value"] == "漫画"

    def test_leaving_the_editor_lands_on_the_tab_rather_than_a_404(
        self, tmp_path: Path
    ) -> None:
        """The reported bug: 取消编辑 answered 「设置分区不存在」.

        The link was written `section='auto_approval'` with an underscore while
        the section is `auto-approval` with a hyphen, and `settings_section` 404s
        on anything not in `SETTINGS_SECTIONS` -- so the only way out of edit mode
        that was not 保存 was guaranteed to be an error page.
        """
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        asyncio.run(
            database.save_auto_approval_rule(
                rule_id=None,
                name="Cancellable",
                enabled=True,
                priority=50,
                condition={
                    "kind": "condition",
                    "field": "Category",
                    "operator": "=",
                    "value": "漫画",
                },
                dsl_snapshot='{Category} = "漫画"',
            )
        )
        rule_id = asyncio.run(database.list_auto_approval_rules())[0].rule_id

        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            editing = client.get(f"/auto-approval-rules/{rule_id}/edit")
            target = re.search(
                r'href="([^"]+)"[^>]*>取消编辑', editing.text
            )
            assert target is not None
            landed = client.get(target.group(1))

        assert landed.status_code == 200
        # And the rule is still there: 取消编辑 abandons the edit, it does not
        # delete anything.
        assert "Cancellable" in landed.text

    def test_a_rule_takes_as_many_conditions_as_the_operator_writes(
        self, tmp_path: Path
    ) -> None:
        """The reported limit of three was the Jinja loop, not the rule engine.

        The AST takes any number of children and `_parse_rule_condition` walks
        whatever the form sends, so this posts five parallel rows and expects one
        group of five back.
        """
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            saved = client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "Five Fields",
                    "priority": "20",
                    "enabled": "on",
                    "condition_field": [
                        "Category",
                        "Language",
                        "Artist",
                        "Pages",
                        "Rating",
                    ],
                    "condition_operator": ["=", "=", "LIKE", ">=", ">="],
                    "condition_value": ["漫画", "中文", "Someone", "20", "4"],
                },
                follow_redirects=False,
            )

        assert saved.status_code == 303
        rules = asyncio.run(database.list_auto_approval_rules())
        assert len(rules) == 1
        condition = rules[0].condition
        assert condition["kind"] == "group"
        assert len(condition["children"]) == 5
        assert [child["field"] for child in condition["children"]] == [
            "Category",
            "Language",
            "Artist",
            "Pages",
            "Rating",
        ]

    def test_the_editor_offers_a_way_to_add_and_remove_condition_rows(
        self, tmp_path: Path
    ) -> None:
        """The controls `settings.js` needs, asserted on the rendered markup.

        Only the hooks are checked here, not the scripted behaviour: the no-JS
        path is the spare row the loop always renders, and these attributes are
        the contract between the template and the script.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/settings/auto-approval")

        assert "data-condition-rows" in page.text
        assert "data-condition-add" in page.text
        assert "data-row-remove" in page.text
        # Still three rows without a script, so a rule can be grown one
        # condition at a time by saving and reopening. Counted with the closing
        # bracket, or `data-condition-rows` on the container matches too.
        assert page.text.count("data-condition-row>") >= 3

    def test_a_rule_of_many_conditions_reopens_with_all_of_them(
        self, tmp_path: Path
    ) -> None:
        """Editing must not be where a five-condition rule loses two of them."""
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "Four Fields",
                    "priority": "20",
                    "enabled": "on",
                    "condition_field": ["Category", "Language", "Pages", "Rating"],
                    "condition_operator": ["=", "=", ">=", ">="],
                    "condition_value": ["漫画", "中文", "20", "4"],
                },
                follow_redirects=False,
            )
            rule_id = asyncio.run(database.list_auto_approval_rules())[0].rule_id
            editing = client.get(f"/auto-approval-rules/{rule_id}/edit")

        assert editing.status_code == 200
        rows = editing.context["edit_rule"]["rows"]
        assert len(rows) == 4
        # One spare row past the four, which is what makes the no-JS path work.
        assert editing.text.count("data-condition-row>") == 5

    def test_a_rule_can_be_deleted(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/auto-approval-rules",
                data={
                    "csrf_token": csrf,
                    "name": "Doomed",
                    "priority": "50",
                    "condition_field": ["Category"],
                    "condition_operator": ["="],
                    "condition_value": ["同人志"],
                },
                follow_redirects=False,
            )
            rule_id = asyncio.run(database.list_auto_approval_rules())[0].rule_id
            deleted = client.post(
                f"/auto-approval-rules/{rule_id}/delete",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            assert deleted.status_code == 303
            missing = client.post(
                f"/auto-approval-rules/{rule_id}/delete",
                data={"csrf_token": csrf},
            )
            # Deleting a rule that is already gone is a 404, not a silent 303:
            # a stale tab must not report success for a rule it cannot see.
            assert missing.status_code == 404

        assert asyncio.run(database.list_auto_approval_rules()) == ()

    def test_editing_a_nested_rule_says_so_instead_of_flattening_it(
        self, tmp_path: Path
    ) -> None:
        """A rule written straight into the database can nest groups.

        The flat editor cannot express that, and flattening it would change what
        the rule matches while keeping its name -- so the tab renders, says this
        one cannot be edited, and leaves 删除 as the way out.
        """
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        nested = {
            "kind": "group",
            "operator": "AND",
            "children": [
                {
                    "kind": "group",
                    "operator": "OR",
                    "children": [
                        {
                            "kind": "condition",
                            "field": "Category",
                            "operator": "=",
                            "value": "同人志",
                        }
                    ],
                }
            ],
        }
        asyncio.run(
            database.save_auto_approval_rule(
                rule_id=None,
                name="Nested",
                enabled=True,
                priority=50,
                condition=nested,
                dsl_snapshot="(Category = 同人志)",
            )
        )
        rule_id = asyncio.run(database.list_auto_approval_rules())[0].rule_id

        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get(f"/auto-approval-rules/{rule_id}/edit")

        assert page.status_code == 200
        assert page.context["edit_unsupported"] is True
        assert "edit_rule" not in page.context

    def test_a_rule_with_an_unknown_operator_is_refused_at_save(
        self, tmp_path: Path
    ) -> None:
        """The acceptance criterion: an invalid rule is refused at save, and nothing is stored.

        `settings.js` previews in the browser, but that is a courtesy -- this
        posts straight past it, which is what a script-off browser and a curl
        call both do. `CONTAINS` was the old DSL, and it has no place here.
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
                    "condition_field": ["Title"],
                    "condition_operator": ["CONTAINS"],
                    "condition_value": ["futa"],
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
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["A Title"],
                },
            )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
#  Paths tab — routing rules
# ---------------------------------------------------------------------------

#: One valid condition the saves and dry runs below post to the path editor.
_CONDITION_ONLY_DOUJINSHI = {
    "condition_field": ["Category"],
    "condition_operator": ["="],
    "condition_value": ["同人志"],
}


class TestPathRules:
    """The paths tab's rule editor: same gate as auto-approval, plus a template.

    The forms submit to `/archive-settings/paths/rules/*` because the paths tab
    has its own set of endpoints -- mirroring the auto-approval tab's editor but
    carrying `path_template` as the extra answer each rule makes.
    """

    def test_a_path_rule_saves_and_appears_on_the_tab(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            saved = client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": csrf,
                    "name": "Doujinshi shelf",
                    "priority": "50",
                    "enabled": "on",
                    "path_template": "同人志/{title}",
                    **_CONDITION_ONLY_DOUJINSHI,
                },
                follow_redirects=False,
            )
            page = client.get("/settings/paths")

        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings/paths"
        assert "Doujinshi shelf" in page.text
        assert "同人志/{title}" in page.text

    def test_a_path_rule_can_be_edited_in_place(self, tmp_path: Path) -> None:
        """编辑 loads every rule field into the editor; 保存 overwrites, never
        appends -- the same bump-and-rewrite path `_parse_rule_condition` serves."""
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": csrf,
                    "name": "Doujinshi shelf",
                    "priority": "50",
                    "enabled": "on",
                    "path_template": "同人志/{title}",
                    "case_sensitive": "on",
                    **_CONDITION_ONLY_DOUJINSHI,
                },
                follow_redirects=False,
            )
            rules = asyncio.run(database.list_archive_path_rules())
            rule_id = rules[0].rule_id

            editing = client.get(f"/archive-settings/paths/rules/{rule_id}/edit")
            assert editing.status_code == 200
            assert editing.context["edit_rule"]["name"] == "Doujinshi shelf"
            assert editing.context["edit_rule"]["path_template"] == "同人志/{title}"
            assert editing.context["edit_rule"]["case_sensitive"] is True
            assert editing.context["edit_rule"]["rows"][0]["value"] == "同人志"

            saved = client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": editing.context["csrf_token"],
                    "path_rule_id": str(rule_id),
                    "name": "Manga shelf",
                    "priority": "10",
                    "enabled": "on",
                    "path_template": "manga/{title}",
                    **{
                        "condition_field": ["Category"],
                        "condition_operator": ["="],
                        "condition_value": ["Manga"],
                    },
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303

        rules = asyncio.run(database.list_archive_path_rules())
        assert len(rules) == 1
        assert rules[0].rule_id == rule_id
        assert rules[0].name == "Manga shelf"
        assert rules[0].priority == 10
        assert rules[0].case_sensitive is False
        assert rules[0].path_template == "manga/{title}"
        assert rules[0].condition["value"] == "Manga"

    def test_toggle_and_delete_take_effect(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": csrf,
                    "name": "Shelf",
                    "priority": "50",
                    "path_template": "{title}",
                    **_CONDITION_ONLY_DOUJINSHI,
                },
                follow_redirects=False,
            )
            rule_id = asyncio.run(database.list_archive_path_rules())[0].rule_id

            client.post(
                f"/archive-settings/paths/rules/{rule_id}/toggle",
                data={"csrf_token": csrf, "enabled": "off"},
            )
            rule = asyncio.run(database.get_archive_path_rule(rule_id))
            assert rule is not None and rule.enabled is False

            client.post(
                f"/archive-settings/paths/rules/{rule_id}/delete",
                data={"csrf_token": csrf},
            )
        assert asyncio.run(database.get_archive_path_rule(rule_id)) is None

    def test_dry_run_reports_which_works_would_match(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        _seed_candidate(database, "A Title")

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/rules/dry-run",
                data={
                    "csrf_token": csrf,
                    "path_template": "x/{title}",
                    "condition_field": ["Title"],
                    "condition_operator": ["="],
                    "condition_value": ["A Title"],
                },
            )

        assert page.status_code == 200
        assert page.context["dry_run"]["matched"] == 1

    def test_an_unsafe_template_is_refused_on_the_page(self, tmp_path: Path) -> None:
        """A rule whose template could escape the library is the one wrong
        answer the whole gate exists to stop, and it is refused while the
        operator is watching -- not at pack time."""
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": csrf,
                    "name": "Escape",
                    "priority": "50",
                    "path_template": "../{title}",
                    **_CONDITION_ONLY_DOUJINSHI,
                },
            )

        assert page.status_code == 400
        assert page.context["error"]
        with sqlite3.connect(database.path) as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM archive_path_rules"
            ).fetchone()[0]
        assert stored == 0

    def test_a_rule_without_a_condition_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": csrf,
                    "name": "No condition",
                    "priority": "50",
                    "path_template": "{title}",
                    "condition_field": [""],
                    "condition_operator": ["="],
                    "condition_value": [""],
                },
            )
        assert page.status_code == 400

    def test_the_cancel_link_lands_back_on_the_tab(self, tmp_path: Path) -> None:
        """取消编辑 is a real exit -- the section's own path, not a mistyped one."""
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())

        with TestClient(create_app(settings)) as client:
            csrf = _authenticate(client, settings)
            client.post(
                "/archive-settings/paths/rules",
                data={
                    "csrf_token": csrf,
                    "name": "Shelf",
                    "priority": "50",
                    "path_template": "{title}",
                    **_CONDITION_ONLY_DOUJINSHI,
                },
                follow_redirects=False,
            )
            rule_id = asyncio.run(database.list_archive_path_rules())[0].rule_id
            editing = client.get(f"/archive-settings/paths/rules/{rule_id}/edit")

        # The cancel link points at the paths tab, the section this editor
        # renders inside -- the mistyped-URL trap the auto-approval tab had.
        assert 'href="/settings/paths"' in editing.text


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

    def test_the_paths_endpoint_carries_the_rule_editor_vocabulary(
        self, tmp_path: Path
    ) -> None:
        """The paths tab needs the rule editor's word lists and the empty rule
        list; the JSON endpoint must report them the same way the page reads
        them."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            payload = client.get("/api/v1/settings/paths").json()

        assert payload["path_rules"] == []
        assert payload["dry_run_scan_limit"] > 0
        assert "fields" in payload["vocabulary"]
        assert "operators" in payload["vocabulary"]
        assert {"title_sources", "template", "paths"} <= set(payload)


# ---------------------------------------------------------------------------
#  一键重新归档
# ---------------------------------------------------------------------------


def _seed_work(
    settings: Settings,
    candidate_id: int,
    *,
    title: str,
    pack_state: str | None = None,
    cbz_relative: str | None = None,
    pinned_path: str | None = None,
    pinned_is_manual: bool = False,
) -> str | None:
    """One downloaded work, with its files really on disk.

    Seeded rather than downloaded: the download worker does not claim a
    COMPLETED row, so the fixtures stay put while the re-archive sweep runs over
    them -- the same trick `test_downloaded_web.py` uses, and for the same
    reason. Returns the published CBZ's absolute path, when there is one.
    """
    database = Database(settings.data_path / "ehbot.db")
    settings.library_path.mkdir(parents=True, exist_ok=True)
    settings.work_path.mkdir(parents=True, exist_ok=True)
    archive = settings.work_path / f"source-{candidate_id}.zip"
    archive.write_bytes(b"archive payload")
    published: str | None = None
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (?, 'DOWNLOADED')",
            (candidate_id,),
        )
        connection.execute(
            "INSERT INTO metadata_values (candidate_id, field_name, field_value,"
            " value_source, confidence, is_manual) "
            "VALUES (?, 'Title', ?, 'EXHENTAI', 0.9, 0)",
            (candidate_id, title),
        )
        job_id = int(
            connection.execute(
                "INSERT INTO download_jobs (candidate_id, idempotency_key, "
                "provider, state, details_json) "
                "VALUES (?, ?, 'TELEGRAM', 'COMPLETED', '{}')",
                (candidate_id, f"seed:{candidate_id}"),
            ).lastrowid
        )
        connection.execute(
            "INSERT INTO artifacts (job_id, artifact_type, path, size_bytes) "
            "VALUES (?, 'ARCHIVE', ?, ?)",
            (job_id, str(archive), archive.stat().st_size),
        )
        if pack_state is not None:
            pack_id = int(
                connection.execute(
                    "INSERT INTO download_jobs (candidate_id, idempotency_key, "
                    "provider, state, details_json) "
                    "VALUES (?, ?, 'CONVERSION', ?, '{}')",
                    (candidate_id, f"convert:{candidate_id}", pack_state),
                ).lastrowid
            )
            if cbz_relative is not None:
                cbz = settings.library_path / cbz_relative
                cbz.parent.mkdir(parents=True, exist_ok=True)
                cbz.write_bytes(b"cbz payload")
                published = str(cbz)
                connection.execute(
                    "INSERT INTO artifacts (job_id, artifact_type, path, "
                    "size_bytes, page_count, library_relative_path) "
                    "VALUES (?, 'CBZ', ?, ?, 12, ?)",
                    (pack_id, published, cbz.stat().st_size, cbz_relative),
                )
        if pinned_path is not None:
            connection.execute(
                "INSERT INTO work_archive_paths (candidate_id, relative_path, "
                "is_manual, operator_name) VALUES (?, ?, ?, 'test')",
                (candidate_id, pinned_path, 1 if pinned_is_manual else 0),
            )
    return published


class TestReArchive:
    """一键重新归档, on the tab that owns the path template.

    The button lives here rather than on `/downloaded` because the template is
    what it applies: an operator who edits the layout wants the books already
    filed under the old one to follow it.
    """

    def test_the_button_reports_a_run_that_had_nothing_to_do(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "paths")
            page = client.post(
                "/archive-settings/paths/rearchive", data={"csrf_token": csrf}
            )

        assert page.status_code == 200
        assert "没有需要重新归档的" in page.text

    def test_an_unarchived_work_is_pinned_and_queued(self, tmp_path: Path) -> None:
        """The book was downloaded and never packed; the button files and queues it."""
        settings = _settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            _authenticate(client, settings)
            _seed_work(settings, 1, title="未打包作品")
            csrf = _csrf(client, "paths")
            response = client.post(
                "/archive-settings/paths/rearchive", data={"csrf_token": csrf}
            )
            assert response.status_code == 200
        # Read after shutdown: the conversion worker claims PENDING rows, and a
        # live one would make the state below a race.
        database = Database(settings.data_path / "ehbot.db")

        assert "入队打包 1 件" in response.text
        assert asyncio.run(database.archive_path_pin(1))["relative_path"] == (
            "未打包作品.cbz"
        )
        with sqlite3.connect(database.path) as connection:
            state = connection.execute(
                "SELECT state FROM download_jobs WHERE idempotency_key = ?",
                ("convert:1",),
            ).fetchone()
        assert state is not None

    def test_a_packed_work_whose_path_changed_is_moved_not_repacked(
        self, tmp_path: Path
    ) -> None:
        """The efficiency rule: a packed book is a file move, never a second pack."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            published = _seed_work(
                settings,
                1,
                title="已打包作品",
                pack_state="CONVERSION_COMPLETED",
                cbz_relative="旧/已打包作品.cbz",
            )
            csrf = _csrf(client, "paths")
            response = client.post(
                "/archive-settings/paths/rearchive", data={"csrf_token": csrf}
            )

        assert response.status_code == 200
        assert "移动文件 1 件" in response.text
        assert not (settings.library_path / "旧" / "已打包作品.cbz").exists()
        assert (settings.library_path / "已打包作品.cbz").exists()
        # Never repacked: the finished packing task is not requeued.
        database = Database(settings.data_path / "ehbot.db")
        with sqlite3.connect(database.path) as connection:
            attempts = connection.execute(
                "SELECT attempt_count FROM download_jobs "
                "WHERE idempotency_key = ?",
                ("convert:1",),
            ).fetchone()[0]
        assert attempts == 0
        assert published is not None

    def test_a_hand_named_path_survives_the_default_run(self, tmp_path: Path) -> None:
        """「模板是默认值」 -- and the run says so instead of silently skipping."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _seed_work(
                settings,
                1,
                title="手动作品",
                pack_state="CONVERSION_COMPLETED",
                cbz_relative="亲手/手动作品.cbz",
                pinned_path="亲手/手动作品.cbz",
                pinned_is_manual=True,
            )
            csrf = _csrf(client, "paths")
            response = client.post(
                "/archive-settings/paths/rearchive", data={"csrf_token": csrf}
            )

        assert "跳过 1 件" in response.text
        assert (settings.library_path / "亲手" / "手动作品.cbz").exists()

    def test_the_force_option_recomputes_a_hand_named_path(
        self, tmp_path: Path
    ) -> None:
        """The whole difference between the two buttons, in one assertion."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _seed_work(
                settings,
                1,
                title="手动作品",
                pack_state="CONVERSION_COMPLETED",
                cbz_relative="亲手/手动作品.cbz",
                pinned_path="亲手/手动作品.cbz",
                pinned_is_manual=True,
            )
            csrf = _csrf(client, "paths")
            response = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": csrf, "force": "on"},
            )

        assert response.status_code == 200
        assert "强制" in response.text
        assert not (settings.library_path / "亲手" / "手动作品.cbz").exists()
        moved = settings.library_path / "手动作品.cbz"
        assert moved.exists()
        database = Database(settings.data_path / "ehbot.db")
        pin = asyncio.run(database.archive_path_pin(1))
        assert pin["relative_path"] == "手动作品.cbz"
        # The override drops the flag, so the next template change may re-file it.
        assert pin["is_manual"] is False

    def test_an_unauthenticated_caller_is_sent_to_login(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": "whatever"},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/login")


# ---------------------------------------------------------------------------
#  AI 供应商 tab
# ---------------------------------------------------------------------------


class _AiTransport:
    """A MockTransport for the AI tab's two endpoints.

    Stateful rather than fixed, because the interesting assertions are about
    *what was sent*: the verification request must be a real chat completion
    (not a `GET /v1/models`), and the key must ride in the Authorization header.
    """

    def __init__(
        self, *, chat_status: int = 200, chat_error: str = "bad key"
    ) -> None:
        self.chat_status = chat_status
        self.chat_error = chat_error
        self.chat_bodies: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "alpha"}, {"id": "beta"}]}
            )
        self.chat_bodies.append(json.loads(request.content))
        if self.chat_status >= 400:
            return httpx.Response(
                self.chat_status, json={"error": {"message": self.chat_error}}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}
        )


def _ai_app(settings: Settings, stub: _AiTransport):
    return create_app(settings, ai_transport=httpx.MockTransport(stub.handler))


def _add_provider(client: TestClient, csrf: str, **overrides: str) -> None:
    data = {
        "csrf_token": csrf,
        "name": "本地",
        "code": "openai",
        "base_url": "http://localhost:11434/v1",
        "timeout_seconds": "30",
        "max_retries": "1",
        "enabled": "on",
    }
    data.update(overrides)
    response = client.post("/settings/ai/providers", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text[:400]


class TestAISettings:
    """AstrBot 式两层管理：左边供应商、右边配置/Key/模型，下面是全局默认模型。"""

    def test_the_tab_renders(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        stub = _AiTransport()
        with TestClient(_ai_app(settings, stub)) as client:
            _authenticate(client, settings)
            page = client.get("/settings/ai")
        assert page.status_code == 200
        assert "全局默认模型" in page.text
        assert "新增供应商" in page.text

    def test_an_unauthenticated_caller_is_sent_to_login(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            response = client.post(
                "/settings/ai/providers",
                data={"csrf_token": "whatever", "name": "x"},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/login")

    def test_no_form_is_nested_inside_another(self, tmp_path: Path) -> None:
        """The browser silently drops a nested form, taking its button with it.

        This tab is the most form-dense page in the app -- a save form, and per
        provider a key form, a model form, a params form and a fetch result --
        so the rule the browser enforces rather than Python is asserted here.
        """
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": "m"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/models/1/params",
                data={"csrf_token": csrf, "params": '{"max_tokens": 64}'},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/chain/append",
                data={"csrf_token": csrf, "model_id": "1"},
                follow_redirects=False,
            )
            page = client.get("/settings/ai").text
        assert nested_form_lines(page) == []

    def test_a_bad_address_renders_the_tab_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            response = client.post(
                "/settings/ai/providers",
                data={
                    "csrf_token": _csrf(client, "ai"),
                    "name": "坏",
                    "code": "openai",
                    "base_url": "not-a-url",
                },
            )
        assert response.status_code == 400
        assert "http://" in response.text

    def test_a_duplicate_name_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            response = client.post(
                "/settings/ai/providers",
                data={
                    "csrf_token": csrf,
                    "name": "本地",
                    "code": "openai",
                    "base_url": "http://localhost:11434/v1",
                },
            )
        assert response.status_code == 400
        assert "已被占用" in response.text

    def test_the_full_path_from_empty_tab_to_primary_model(
        self, tmp_path: Path
    ) -> None:
        """Add provider → key → model → test → 主力, through the real forms.

        This is the wiring test: every form's field names have to match the
        handler's, and the model must be usable *without* a prior test -- the
        gate is gone on purpose (an unreachable endpoint must still be
        configurable), and 「测试」 is a button rather than a prerequisite.
        """
        settings = _settings(tmp_path)
        stub = _AiTransport()
        with TestClient(_ai_app(settings, stub)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            response = client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "主:sk-live-secret-0001"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            response = client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": "gpt-4o-mini"},
                follow_redirects=False,
            )
            assert response.status_code == 303

            # No test first: saving it as the primary works anyway.
            response = client.post(
                "/settings/ai/chain/primary",
                data={"csrf_token": csrf, "model_id": "1"},
                follow_redirects=False,
            )
            assert response.status_code == 303

            page = client.get("/settings/ai")
            assert "未验证" in page.text
            assert "gpt-4o-mini" in page.text

            response = client.post(
                "/settings/ai/models/1/verify", data={"csrf_token": csrf}
            )
            assert response.status_code == 200
            assert "验证通过" in response.text
            # Verification is a real chat request, not just a listing.
            assert stub.chat_bodies
            assert stub.chat_bodies[0]["messages"][0]["role"] == "system"
            assert stub.requests[-1].headers["authorization"] == (
                "Bearer sk-live-secret-0001"
            )
            # 默认请求体只有 model 与 messages：写死 temperature/max_tokens 会让
            # 推理模型永远验证失败。
            assert set(stub.chat_bodies[0]) == {"model", "messages"}

            page = client.get("/settings/ai")
        assert "主力" in page.text
        assert "已验证" in page.text

    def test_a_pasted_block_of_keys_and_a_model_checklist_land_in_one_submit(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            response = client.post(
                "/settings/ai/providers/1/keys",
                data={
                    "csrf_token": csrf,
                    "api_keys": "主:sk-one\n备用:sk-two\n# 注释\nsk-three",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            response = client.post(
                "/settings/ai/providers/1/models",
                data={
                    "csrf_token": csrf,
                    "model_name": ["alpha", "beta", "alpha"],
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
        assert "Key 3/3 可用" in page.text
        # alpha 被勾了两次：模型清单去重后是 2 个。
        assert "模型 2/2 启用" in page.text
        assert "主" in page.text and "备用" in page.text
        assert "alpha" in page.text and "beta" in page.text

    def test_a_key_is_never_rendered_back(self, tmp_path: Path) -> None:
        """The page may show a label and a state; it must never show the key.

        Checked on a normal render and on a rejected save, because a value
        echoed back into a re-rendered form is exactly how a credential leaks
        out of a page that otherwise never prints one. The textarea that keys
        are pasted into is empty on every render.
        """
        settings = _settings(tmp_path)
        secret = "sk-live-secret-abcdef"
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": f"主:{secret}"},
                follow_redirects=False,
            )
            page = client.get("/settings/ai")
            assert secret not in page.text
            assert "主" in page.text
            rejected = client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "",
                      },
            )
        assert rejected.status_code == 400
        assert secret not in rejected.text

    def test_an_empty_key_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            response = client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "  "},
            )
        assert response.status_code == 400
        assert "不能为空" in response.text

    def test_a_failed_verification_is_reported_and_stored(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        stub = _AiTransport(chat_status=401)
        with TestClient(_ai_app(settings, stub)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": "m"},
                follow_redirects=False,
            )
            response = client.post(
                "/settings/ai/models/1/verify", data={"csrf_token": csrf}
            )
            assert response.status_code == 400
            assert "验证失败" in response.text
            page = client.get("/settings/ai")
        assert "验证失败" in page.text
        assert "AI_AUTH" in page.text

    def test_a_parameter_refusal_tells_the_operator_to_change_the_params(
        self, tmp_path: Path
    ) -> None:
        """推理模型的 400 不该被说成「地址或 Key 错了」。"""
        settings = _settings(tmp_path)
        stub = _AiTransport(
            chat_status=400,
            chat_error="Unsupported parameter: 'temperature' is not supported.",
        )
        with TestClient(_ai_app(settings, stub)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf, default_params='{"temperature": 0.5}')
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": "m"},
                follow_redirects=False,
            )
            response = client.post(
                "/settings/ai/models/1/verify", data={"csrf_token": csrf}
            )
        assert response.status_code == 400
        assert "参数" in response.text
        assert "AI_PARAM_REJECTED" in response.text

    def test_a_models_own_params_are_editable_from_its_row(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        stub = _AiTransport()
        with TestClient(_ai_app(settings, stub)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": "o3-mini"},
                follow_redirects=False,
            )
            response = client.post(
                "/settings/ai/models/1/params",
                data={
                    "csrf_token": csrf,
                    "params": '{"max_completion_tokens": 512}',
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            client.post("/settings/ai/models/1/verify", data={"csrf_token": csrf})
        assert stub.chat_bodies[-1]["max_completion_tokens"] == 512
        assert "temperature" not in stub.chat_bodies[-1]

    def test_fetching_models_renders_a_checklist_that_adds_selected(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        stub = _AiTransport()
        with TestClient(_ai_app(settings, stub)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            page = client.post(
                "/settings/ai/providers/1/models/fetch",
                data={"csrf_token": csrf},
            )
            assert page.status_code == 200
            assert "alpha" in page.text and "beta" in page.text
            # The list is offered, not auto-added.
            assert stub.requests[-1].url.path.endswith("/models")

            response = client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": ["alpha", "beta"]},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
        assert "alpha" in page.text
        assert "beta" in page.text

    def test_the_selected_provider_survives_a_save(self, tmp_path: Path) -> None:
        """?provider= 是右侧面板的唯一真相，保存后回到同一个供应商。"""
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            response = client.post(
                "/settings/ai/providers",
                data={
                    "csrf_token": csrf,
                    "name": "第二个",
                    "code": "openai",
                    "base_url": "http://localhost:1234/v1",
                },
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/settings/ai?provider=2")

    def test_chain_reorder_remove_and_promote(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            for name in ("a", "b"):
                client.post(
                    "/settings/ai/providers/1/models",
                    data={"csrf_token": csrf, "model_name": name},
                    follow_redirects=False,
                )
            for model_id in (1, 2):
                client.post(
                    "/settings/ai/chain/append",
                    data={"csrf_token": csrf, "model_id": str(model_id)},
                    follow_redirects=False,
                )
            page = client.get("/settings/ai")
            assert "主力 · 本地 / a" in page.text

            response = client.post(
                "/settings/ai/chain/shift",
                data={"csrf_token": csrf, "model_id": "2", "delta": "-1"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
            assert "主力 · 本地 / b" in page.text

            response = client.post(
                "/settings/ai/chain/primary",
                data={"csrf_token": csrf, "model_id": "1"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
            assert "主力 · 本地 / a" in page.text

            response = client.post(
                "/settings/ai/chain/remove",
                data={"csrf_token": csrf, "model_id": "2"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
        # b is gone from the chain; its label survives only in the 「加入备用」
        # dropdown, which is not a chain entry.
        assert "主力 · 本地 / a" in page.text
        assert "主力 · 本地 / b" not in page.text
        assert "备用 1 · 本地 / b" not in page.text

    def test_a_disabled_key_can_be_re_enabled_and_its_cooldown_reset(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            response = client.post(
                "/settings/ai/keys/1/toggle",
                data={"csrf_token": csrf, "enabled": "off"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert "Key 0/1 可用" in client.get("/settings/ai").text
            response = client.post(
                "/settings/ai/keys/1/reset",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            assert response.status_code == 303
            response = client.post(
                "/settings/ai/keys/1/toggle",
                data={"csrf_token": csrf, "enabled": "on"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
        assert "Key 1/1 可用" in page.text

    def test_deleting_a_provider_removes_its_chain_entry(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "ai")
            _add_provider(client, csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/providers/1/models",
                data={"csrf_token": csrf, "model_name": "m"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/chain/append",
                data={"csrf_token": csrf, "model_id": "1"},
                follow_redirects=False,
            )
            response = client.post(
                "/settings/ai/providers/1/delete",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/ai")
        assert "还没有 AI 供应商" in page.text
        assert "主力 · 本地 / m" not in page.text


# ---------------------------------------------------------------------------
#  Path tab — the 路径来源 switch and the AI sub-panel (R29)
# ---------------------------------------------------------------------------


def _paths_ai_data(**overrides: str) -> dict[str, str]:
    data = {
        "path_source": "ai",
        "ai_prompt": "自定义提示词",
        "ai_batch_size": "30",
        "ai_concurrency": "3",
        "ai_fallback_to_rules": "on",
        "ai_default_include_current": "on",
    }
    data.update(overrides)
    return data


class TestAIPathsSettings:
    def test_the_tab_renders_the_source_choice_and_the_reference_prompt(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/settings/paths")
        assert page.status_code == 200
        assert "归档路径来源" in page.text
        assert "模板与规则" in page.text
        # The default prompt is the reference text, and the panel is rendered
        # hidden while the template mode is the stored choice.
        assert "你是 EhBot 的书库整理助手。" in page.text
        assert "data-ai-panel hidden" in page.text

    def test_saving_ai_mode_stores_every_field(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            response = client.post(
                "/archive-settings/paths/ai",
                data={"csrf_token": _csrf(client, "paths"), **_paths_ai_data()},
                follow_redirects=False,
            )
            assert response.status_code == 303
            page = client.get("/settings/paths")
        assert page.context["path_source"] == "ai"
        assert 'value="ai"' in page.text
        assert "自定义提示词" in page.text
        # The template side is visibly out of effect.
        assert "data-ai-inactive" in page.text
        assert "兜底模板" in page.text

        stored = asyncio.run(database.archive_settings())
        assert stored["path_source"] == "ai"
        assert stored["ai_prompt"] == "自定义提示词"
        assert stored["ai_batch_size"] == "30"
        assert stored["ai_concurrency"] == "3"
        assert stored["ai_fallback_to_rules"] == "1"
        assert stored["ai_default_include_current"] == "1"

    def test_the_model_source_panel_and_its_editor_render(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            page = client.get("/settings/paths")
        assert page.status_code == 200
        assert "路径决策模型" in page.text
        assert "跟随全局默认" in page.text
        assert "本页单独指定" in page.text
        # 没有模型时两台编辑器都给出同一句指引（宏是共用的）。
        assert "还没有模型：先在「设置 → AI 供应商」添加供应商与模型" in page.text

    def test_a_custom_list_is_kept_separate_from_the_global_default(
        self, tmp_path: Path
    ) -> None:
        """路径页自选模型：全局默认不动，本页的列表生效。"""
        settings = _settings(tmp_path)
        with TestClient(_ai_app(settings, _AiTransport())) as client:
            _authenticate(client, settings)
            ai_csrf = _csrf(client, "ai")
            _add_provider(client, ai_csrf)
            client.post(
                "/settings/ai/providers/1/keys",
                data={"csrf_token": ai_csrf, "api_keys": "k"},
                follow_redirects=False,
            )
            for name in ("big", "small"):
                client.post(
                    "/settings/ai/providers/1/models",
                    data={"csrf_token": ai_csrf, "model_name": name},
                    follow_redirects=False,
                )
            client.post(
                "/settings/ai/chain/primary",
                data={"csrf_token": ai_csrf, "model_id": "1"},
                follow_redirects=False,
            )
            client.post(
                "/settings/ai/chain/append",
                data={"csrf_token": ai_csrf, "model_id": "2"},
                follow_redirects=False,
            )

            paths_csrf = _csrf(client, "paths")
            response = client.post(
                "/archive-settings/paths/ai/models",
                data={"csrf_token": paths_csrf, "ai_model_source": "custom"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            response = client.post(
                "/settings/paths/chain/primary",
                data={"csrf_token": paths_csrf, "model_id": "2"},
                follow_redirects=False,
            )
            assert response.status_code == 303

            page = client.get("/settings/paths")
            assert "本页单独指定" in page.text
            assert "主力 本地 / small" in page.text
            assert "跟随全局默认" in page.text  # 仍是可选的一项

            # 切回跟随：本页列表还在，但生效的是全局默认。
            client.post(
                "/archive-settings/paths/ai/models",
                data={"csrf_token": paths_csrf, "ai_model_source": "default"},
                follow_redirects=False,
            )
            page = client.get("/settings/paths")
            assert "主力 本地 / big" in page.text

            ai_page = client.get("/settings/ai")
        # 全局默认从头到尾没被动过。
        assert "主力 · 本地 / big" in ai_page.text
        assert "备用 1 · 本地 / small" in ai_page.text

    def test_an_out_of_range_batch_size_is_refused_before_the_mode_changes(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            response = client.post(
                "/archive-settings/paths/ai",
                data={
                    "csrf_token": _csrf(client, "paths"),
                    **_paths_ai_data(ai_batch_size="0"),
                },
            )
        assert response.status_code == 400
        assert "每批处理数量" in response.text
        stored = asyncio.run(database.archive_settings())
        # Nothing was written: the numbers are validated first on purpose.
        assert stored.get("path_source") is None

    def test_resetting_the_prompt_returns_to_the_reference_text(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            csrf = _csrf(client, "paths")
            client.post(
                "/archive-settings/paths/ai",
                data={"csrf_token": csrf, **_paths_ai_data()},
                follow_redirects=False,
            )
            client.post(
                "/archive-settings/paths/ai/prompt-default",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            page = client.get("/settings/paths")
        assert "自定义提示词" not in page.text
        assert "你是 EhBot 的书库整理助手。" in page.text

    def test_clearing_the_cache_reports_how_many_rows_were_dropped(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        database = Database(settings.data_path / "ehbot.db")
        asyncio.run(database.initialize())
        with sqlite3.connect(database.path) as connection:
            connection.execute(
                "INSERT INTO candidates (id, status) VALUES (1, 'APPROVED')"
            )
            connection.execute(
                "INSERT INTO ai_path_suggestions (candidate_id, fingerprint, "
                "prompt_hash, relative_path, directory, filename, provider_id, "
                "model_name, attempts) "
                "VALUES (1, 'fp', 'ph', 'a/b.cbz', 'a', 'b', 1, 'm', 1)"
            )

        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/ai/cache/clear",
                data={"csrf_token": _csrf(client, "paths")},
            )
        assert "已清除 1 条" in page.text
        assert asyncio.run(database.ai_path_suggestion_count()) == 0

    def test_clearing_an_empty_cache_says_so_rather_than_zero(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.post(
                "/archive-settings/paths/ai/cache/clear",
                data={"csrf_token": _csrf(client, "paths")},
            )
        assert "已清除 0 条" not in page.text
        assert "没有可清除" in page.text

    def test_no_form_is_nested_inside_another_with_the_ai_panel_shown(
        self, tmp_path: Path
    ) -> None:
        """The paths tab is now two forms side by side; a nested one is dropped."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            client.post(
                "/archive-settings/paths/ai",
                data={
                    "csrf_token": _csrf(client, "paths"),
                    **_paths_ai_data(),
                },
                follow_redirects=False,
            )
            page = client.get("/settings/paths").text
        assert nested_form_lines(page) == []

    def test_an_unauthenticated_caller_is_sent_to_login(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/archive-settings/paths/ai",
                data={"csrf_token": "whatever", "path_source": "ai"},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/login")


class TestReArchiveDryRun:
    """试跑: plan and report, change nothing.

    The reason it exists is 强制's cost in AI mode -- 「全库重新询问」 is one press
    away from a very large bill -- so the assertions below are all 「报告了，但没做」.
    """

    def test_a_template_dry_run_reports_without_pinning_or_queueing(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _seed_work(settings, 1, title="未打包作品")
            response = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": _csrf(client, "paths"), "dry_run": "on"},
            )

        assert response.status_code == 200
        assert "试跑结果" in response.text
        assert "入队打包 1 件" in response.text
        database = Database(settings.data_path / "ehbot.db")
        assert asyncio.run(database.archive_path_pin(1)) is None
        with sqlite3.connect(database.path) as connection:
            row = connection.execute(
                "SELECT 1 FROM download_jobs WHERE idempotency_key = ?",
                ("convert:1",),
            ).fetchone()
        assert row is None

    def test_an_ai_dry_run_queues_nothing_and_moves_nothing(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _seed_work(
                settings,
                1,
                title="已打包作品",
                pack_state="CONVERSION_COMPLETED",
                cbz_relative="旧/已打包作品.cbz",
            )
            client.post(
                "/archive-settings/paths/ai",
                data={"csrf_token": _csrf(client, "paths"), **_paths_ai_data()},
            )
            response = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": _csrf(client, "paths"), "dry_run": "on"},
            )

        assert "试跑结果" in response.text
        assert "入队重算路径 1 件" in response.text
        assert (settings.library_path / "旧" / "已打包作品.cbz").exists()
        # The packed-again question: the finished task keeps its state and its
        # attempt count, i.e. nothing was requeued for a re-pack.
        database = Database(settings.data_path / "ehbot.db")
        with sqlite3.connect(database.path) as connection:
            row = connection.execute(
                "SELECT state, attempt_count, details_json FROM download_jobs "
                "WHERE idempotency_key = ?",
                ("convert:1",),
            ).fetchone()
        assert row[0] == "CONVERSION_COMPLETED"
        assert row[1] == 0


class TestAiReArchiveSweep:
    """AI 模式的一键重新归档：计划只读缓存，需要新答案的交给打包队列."""

    def test_a_packed_book_without_a_current_answer_is_queued_for_a_recompute(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _seed_work(
                settings,
                1,
                title="已打包作品",
                pack_state="CONVERSION_COMPLETED",
                cbz_relative="旧/已打包作品.cbz",
            )
            client.post(
                "/archive-settings/paths/ai",
                data={"csrf_token": _csrf(client, "paths"), **_paths_ai_data()},
            )
            response = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": _csrf(client, "paths")},
            )

        assert response.status_code == 200
        assert "入队重算路径 1 件" in response.text
        # Moved by the job, not by the request: 「已打包不再重新打包」 still holds,
        # and a path-only job cannot repack.
        assert (settings.library_path / "旧" / "已打包作品.cbz").exists()
        database = Database(settings.data_path / "ehbot.db")
        with sqlite3.connect(database.path) as connection:
            row = connection.execute(
                "SELECT state, attempt_count FROM download_jobs "
                "WHERE idempotency_key = ?",
                ("convert:1",),
            ).fetchone()
        # The worker may or may not have claimed it by shutdown, but it never
        # re-packs: a refile job runs no packer, so the attempt count is what
        # could move if the sweep had queued a pack.
        assert row[0] in {
            "CONVERSION_PENDING",
            "CONVERSION_RUNNING",
            "CONVERSION_WAITING_PATH",
            "CONVERSION_FAILED",
        }

    def test_force_covers_a_book_the_default_run_leaves_to_a_hand_named_path(
        self, tmp_path: Path
    ) -> None:
        """「手动指定优先」 is the default's rule; 强制 is the operator overriding it.

        The same book under the same settings, run twice: the first run skips it
        with a reason, the second re-asks the model. That difference is the whole
        of what 强制 means in AI mode.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _seed_work(
                settings,
                1,
                title="已打包作品",
                pack_state="CONVERSION_COMPLETED",
                cbz_relative="旧/已打包作品.cbz",
                pinned_path="手动/名字.cbz",
                pinned_is_manual=True,
            )
            client.post(
                "/archive-settings/paths/ai",
                data={"csrf_token": _csrf(client, "paths"), **_paths_ai_data()},
            )
            default_run = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": _csrf(client, "paths")},
            )
            forced_run = client.post(
                "/archive-settings/paths/rearchive",
                data={"csrf_token": _csrf(client, "paths"), "force": "on"},
            )

        assert default_run.status_code == 200
        assert "跳过 1 件" in default_run.text
        assert "入队重算路径 1 件" not in default_run.text
        assert forced_run.status_code == 200
        assert "强制" in forced_run.text
        assert "入队重算路径 1 件" in forced_run.text
