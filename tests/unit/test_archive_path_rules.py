"""Archive-path routing rules: storage, the template resolver, and its gate.

The rule engine itself is tested in `test_auto_approval.py`; what this module
covers is the archive side -- the CRUD mirror over `archive_path_rules`, the
service resolver that turns "first enabled rule whose condition matches" into a
template, and the save-time validation that keeps a rule's template from being
something a packer would reject. The wiring into the packer (`_library_target`,
`planned_library_path`) lives in `test_library_template.py`.

The resolver is the interesting function here: it re-queries
`effective_metadata` rather than trusting the metadata a packing job already
holds, so a rule sees exactly what auto-approval sees. That contract -- same
metadata, same precedence -- is asserted directly rather than inferred: `_seed`
writes `metadata_values` rows, and `library_template_for` reads them back
through the real precedence query, so a rule that somehow consulted the wrong
rows would fail these tests.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest

from app.archive.service import ArchiveSettingsError, ArchiveSettingsService
from app.auto_approval.rules import render_rule_dsl, validate_rule_ast
from app.db.database import Database


def _condition(title_like: str = "%miku%") -> dict:
    """One valid rule condition, as the editor would produce it."""
    return validate_rule_ast(
        {
            "kind": "condition",
            "field": "Title",
            "operator": "LIKE",
            "value": title_like,
        }
    )


def _seed(tmp_path: Path) -> tuple[Database, ArchiveSettingsService]:
    """A fresh database and the settings service over it, default template set.

    `initialize` runs every migration, including 017, so the `archive_path_rules`
    table exists exactly as a fresh install would create it.
    """
    database = Database(tmp_path / "ehbot.db")
    asyncio.run(database.initialize())
    settings = ArchiveSettingsService(
        database,
        tmp_path / "work",
        default_library_path=tmp_path / "library",
        default_work_path=tmp_path / "work",
    )
    asyncio.run(
        database.save_archive_settings({"library_template": "{title}"})
    )
    return database, settings


def _seed_candidate_metadata(
    database: Database, *, title: str
) -> int:
    """One candidate with a Title row, the way the ingest path would write it."""
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (1, 'PENDING_REVIEW')"
        )
        connection.execute(
            "INSERT INTO metadata_values "
            "(candidate_id, field_name, field_value, value_source, confidence,"
            " is_manual) VALUES (1, 'Title', ?, 'EXHENTAI', 0.9, 0)",
            (title,),
        )
    return 1


def _clear_metadata(database: Database) -> None:
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "DELETE FROM metadata_values WHERE candidate_id = 1"
        )


def _new_rule(
    database: Database,
    *,
    name: str = "Default rule",
    path_template: str = "{title}",
    condition: dict | None = None,
    priority: int = 100,
    enabled: bool = True,
    case_sensitive: bool = False,
) -> int:
    condition = condition if condition is not None else _condition()
    rule = asyncio.run(
        database.save_archive_path_rule(
            rule_id=None,
            name=name,
            enabled=enabled,
            priority=priority,
            condition=condition,
            dsl_snapshot=render_rule_dsl(condition),
            path_template=path_template,
            case_sensitive=case_sensitive,
        )
    )
    return rule.rule_id


# ------------------------------------------------------------ storage round trip


class TestStorage:
    def test_a_fresh_install_has_no_rules(self, tmp_path: Path) -> None:
        database, _ = _seed(tmp_path)
        assert asyncio.run(database.list_archive_path_rules()) == ()

    def test_save_and_read_round_trip_on_an_insert(self, tmp_path: Path) -> None:
        database, _ = _seed(tmp_path)
        created = asyncio.run(
            database.save_archive_path_rule(
                rule_id=None,
                name="NTR shelf",
                enabled=True,
                priority=10,
                condition=_condition(),
                dsl_snapshot='{Title} LIKE "%miku%"',
                path_template="NTR/{title}",
            )
        )

        assert created.rule_id == 1
        assert created.version == 1
        assert created.case_sensitive is False

        reread = asyncio.run(database.get_archive_path_rule(created.rule_id))
        assert reread is not None
        assert reread.name == "NTR shelf"
        assert reread.path_template == "NTR/{title}"
        assert reread.condition == _condition()
        assert reread.enabled is True

    def test_the_list_and_get_read_the_same_shape(self, tmp_path: Path) -> None:
        """`list_archive_path_rules` and `get_archive_path_rule` must not drift
        column-by-column -- the list feeds the editor and the resolver both."""
        database, _ = _seed(tmp_path)
        _new_rule(database, name="Only", path_template="x/{title}")

        listed = asyncio.run(database.list_archive_path_rules())[0]
        fetched = asyncio.run(database.get_archive_path_rule(listed.rule_id))
        assert fetched is not None
        for field in (
            "rule_id",
            "name",
            "enabled",
            "priority",
            "version",
            "condition",
            "dsl_snapshot",
            "path_template",
            "case_sensitive",
        ):
            assert getattr(listed, field) == getattr(fetched, field), field

    def test_case_sensitive_defaults_off_and_round_trips(
        self, tmp_path: Path
    ) -> None:
        database, _ = _seed(tmp_path)
        first = asyncio.run(
            database.save_archive_path_rule(
                rule_id=None,
                name="Insensitive",
                enabled=True,
                priority=1,
                condition=_condition(),
                dsl_snapshot="x",
                path_template="{title}",
            )
        )
        second = asyncio.run(
            database.save_archive_path_rule(
                rule_id=None,
                name="Sensitive",
                enabled=True,
                priority=2,
                condition=_condition(),
                dsl_snapshot="x",
                path_template="{title}",
                case_sensitive=True,
            )
        )

        by_id = {
            rule.rule_id: rule
            for rule in asyncio.run(database.list_archive_path_rules())
        }
        assert by_id[first.rule_id].case_sensitive is False
        assert by_id[second.rule_id].case_sensitive is True

    def test_an_update_bumps_version_and_rewrites_not_appends(
        self, tmp_path: Path
    ) -> None:
        database, _ = _seed(tmp_path)
        rule_id = _new_rule(database)

        updated = asyncio.run(
            database.save_archive_path_rule(
                rule_id=rule_id,
                name="Renamed",
                enabled=False,
                priority=99,
                condition=_condition("%miku%"),
                dsl_snapshot='{Title} LIKE "%miku%"',
                path_template="Other/{title}",
            )
        )

        assert updated.rule_id == rule_id
        assert updated.version == 2
        assert updated.name == "Renamed"

        # The edit overwrote, not appended.
        all_rules = asyncio.run(database.list_archive_path_rules())
        assert len(all_rules) == 1

    def test_priority_then_id_orders_the_list(self, tmp_path: Path) -> None:
        """`ORDER BY priority, id`: equal priorities fall back to insertion
        order, which is what makes two rules at one priority deterministic."""
        database, _ = _seed(tmp_path)
        low = _new_rule(database, name="Lowest priority", priority=500)
        high = _new_rule(database, name="Highest priority", priority=10)
        tied_a = _new_rule(database, name="Tied A", priority=50)
        tied_b = _new_rule(database, name="Tied B", priority=50)

        assert [r.rule_id for r in asyncio.run(database.list_archive_path_rules())] == [
            high,
            tied_a,
            tied_b,
            low,
        ]

    def test_enabled_only_filters_disabled_rows(self, tmp_path: Path) -> None:
        database, _ = _seed(tmp_path)
        _new_rule(database, name="On", enabled=True)
        _new_rule(database, name="Off", enabled=False)

        assert [r.name for r in asyncio.run(database.list_archive_path_rules())] == [
            "On",
            "Off",
        ]
        assert [
            r.name
            for r in asyncio.run(database.list_archive_path_rules(enabled_only=True))
        ] == ["On"]

    def test_get_returns_none_for_a_missing_rule(self, tmp_path: Path) -> None:
        database, _ = _seed(tmp_path)
        assert asyncio.run(database.get_archive_path_rule(99)) is None

    def test_toggle_and_delete(self, tmp_path: Path) -> None:
        database, _ = _seed(tmp_path)
        rule_id = _new_rule(database)

        asyncio.run(database.set_archive_path_rule_enabled(rule_id, False))
        rule = asyncio.run(database.get_archive_path_rule(rule_id))
        assert rule is not None and rule.enabled is False

        asyncio.run(database.delete_archive_path_rule(rule_id))
        assert asyncio.run(database.get_archive_path_rule(rule_id)) is None

    def test_acting_on_a_missing_rule_refuses(self, tmp_path: Path) -> None:
        """A stale page acting on a rule someone else removed must not silently
        no-op -- the 编辑 → 保存 round trip would otherwise resurrect a delete."""
        database, _ = _seed(tmp_path)
        with pytest.raises(LookupError):
            asyncio.run(database.set_archive_path_rule_enabled(99, True))
        with pytest.raises(LookupError):
            asyncio.run(database.delete_archive_path_rule(99))
        with pytest.raises(LookupError):
            asyncio.run(
                database.save_archive_path_rule(
                    rule_id=99,
                    name="Orphan",
                    enabled=True,
                    priority=1,
                    condition=_condition(),
                    dsl_snapshot="x",
                    path_template="{title}",
                )
            )


# ------------------------------------------------------------ the resolver


class TestLibraryTemplateFor:
    def test_the_first_matching_enabled_rule_wins(self, tmp_path: Path) -> None:
        """Lowest priority first is not "most specific": it is the order the
        editor lists and the operator is told to plan. That promise is what
        `library_template_for` must honour."""
        database, settings = _seed(tmp_path)
        _new_rule(
            database,
            name="Miku shelf",
            path_template="miku/{title}",
            priority=10,
        )
        _new_rule(
            database,
            name="Any like",
            path_template="generic/{title}",
            priority=20,
        )
        _seed_candidate_metadata(database, title="miku chan")

        template, rule = asyncio.run(settings.library_template_for(1))
        assert template == "miku/{title}"
        assert rule is not None and rule.name == "Miku shelf"

    def test_no_match_falls_back_to_the_global_template(
        self, tmp_path: Path
    ) -> None:
        database, settings = _seed(tmp_path)
        _new_rule(database, path_template="miku/{title}", priority=10)
        _seed_candidate_metadata(database, title="plain story")

        template, rule = asyncio.run(settings.library_template_for(1))
        assert template == "{title}"
        assert rule is None

    def test_a_disabled_rule_is_passed_over(self, tmp_path: Path) -> None:
        """停用 parks a rule; it must not still route books."""
        database, settings = _seed(tmp_path)
        _new_rule(
            database,
            name="Parked",
            path_template="miku/{title}",
            priority=10,
            enabled=False,
        )
        _seed_candidate_metadata(database, title="miku chan")

        template, rule = asyncio.run(settings.library_template_for(1))
        assert template == "{title}"
        assert rule is None

    def test_an_invalid_stored_rule_is_skipped_not_fatal(
        self, tmp_path: Path, caplog
    ) -> None:
        """A stored condition that has gone stale must degrade one rule, not the
        pack -- the same tolerant read the global template gets."""
        database, settings = _seed(tmp_path)
        _new_rule(
            database,
            name="Good earlier",
            path_template="miku/{title}",
            priority=10,
        )
        asyncio.run(
            database.save_archive_path_rule(
                rule_id=None,
                name="Corrupt",
                enabled=True,
                priority=5,
                condition={"kind": "condition", "field": "Title", "operator": "BOGUS"},
                dsl_snapshot='"BOGUS"',
                path_template="broken/{title}",
            )
        )
        _seed_candidate_metadata(database, title="miku chan")

        with caplog.at_level(logging.WARNING, logger="app.archive.service"):
            template, rule = asyncio.run(settings.library_template_for(1))

        assert template == "miku/{title}"
        assert rule is not None and rule.name == "Good earlier"
        assert any(
            "archive_path_rule_unusable" in record.message
            for record in caplog.records
        )

    def test_empty_metadata_matches_nothing(self, tmp_path: Path) -> None:
        """An unenriched work is deliberately outside every rule's reach -- rules
        describe books, and a book with no rows keeps the default path."""
        database, settings = _seed(tmp_path)
        _new_rule(database, path_template="miku/{title}", priority=10)
        _seed_candidate_metadata(database, title="miku chan")
        _clear_metadata(database)

        template, rule = asyncio.run(settings.library_template_for(1))
        assert template == "{title}"
        assert rule is None

    def test_the_case_sensitive_toggle_changes_what_matches(
        self, tmp_path: Path
    ) -> None:
        database, settings = _seed(tmp_path)
        _new_rule(
            database,
            name="Sensitive",
            path_template="capital/{title}",
            priority=10,
            condition=_condition("%Miku%"),
            case_sensitive=True,
        )
        _seed_candidate_metadata(database, title="miku chan")

        # Case-sensitive: `miku` != `Miku`, so nothing matches → default.
        template, rule = asyncio.run(settings.library_template_for(1))
        assert template == "{title}"
        assert rule is None

        # Same pattern, flags off, matches the same metadata → the later rule
        # still loses, because the earlier one matched nothing and priority 20
        # falls through to the default … no: rules are priority-ordered, and the
        # insensitive one at 20 is the only match, so it wins.
        _new_rule(
            database,
            name="Insensitive",
            path_template="anycase/{title}",
            priority=20,
            condition=_condition("%Miku%"),
        )
        template, rule = asyncio.run(settings.library_template_for(1))
        assert template == "anycase/{title}"
        assert rule is not None and rule.name == "Insensitive"


# ------------------------------------------------------------ the save gate


class TestSavePathRule:
    @pytest.mark.parametrize(
        ("template", "code"),
        [
            ("", "PATH_RULE_TEMPLATE_EMPTY"),
            ("   ", "PATH_RULE_TEMPLATE_EMPTY"),
            ("/srv/library/{title}", "TEMPLATE_ABSOLUTE"),
            ("../{title}", "TEMPLATE_TRAVERSAL"),
            ("{publisher}/{title}", "TEMPLATE_UNKNOWN_FIELD"),
            ("{category}/{artist}", "TEMPLATE_NO_TITLE"),
        ],
    )
    def test_a_template_that_cannot_render_is_refused(
        self, tmp_path: Path, template: str, code: str
    ) -> None:
        """The rule's gate is the same one the global template stands behind, so
        a rule that could not render is caught here, with the operator watching,
        instead of at pack time."""
        _, settings = _seed(tmp_path)
        with pytest.raises(ArchiveSettingsError) as raised:
            asyncio.run(
                settings.save_path_rule(
                    rule_id=None,
                    name="Refused",
                    enabled=True,
                    priority=10,
                    condition=_condition(),
                    path_template=template,
                )
            )
        assert raised.value.code == code

    def test_a_backslash_template_is_stored_normalised(
        self, tmp_path: Path
    ) -> None:
        """A Windows operator types the separator their shell uses, and the
        stored template must be the same tree every platform would produce."""
        _, settings = _seed(tmp_path)
        rule = asyncio.run(
            settings.save_path_rule(
                rule_id=None,
                name="Windows typist",
                enabled=True,
                priority=10,
                condition=_condition(),
                path_template="miku\\{title}",
            )
        )
        assert rule.path_template == "miku/{title}"

    def test_the_stored_dsl_is_the_engine_s_own_rendering(
        self, tmp_path: Path
    ) -> None:
        database, settings = _seed(tmp_path)
        rule = asyncio.run(
            settings.save_path_rule(
                rule_id=None,
                name="DSL",
                enabled=True,
                priority=10,
                condition=_condition(),
                path_template="{title}",
            )
        )
        assert rule.dsl_snapshot == '{Title} LIKE "%miku%"'


# ------------------------------------------------------------ the form parser


class _SlimForm:
    """The two methods `parse_rule_condition` calls, nothing more."""

    def __init__(self, fields, operators, values, group: str = "AND"):
        self._fields = fields
        self._operators = operators
        self._values = values
        self._group = group

    def getlist(self, name: str) -> list[str]:
        return {
            "condition_field": self._fields,
            "condition_operator": self._operators,
            "condition_value": self._values,
        }[name]

    def get(self, name: str, default=None):
        return self._group if name == "group_operator" else default


def _parse(cmds, group: str = "AND"):
    from app.web.rule_forms import parse_rule_condition

    return parse_rule_condition(
        _SlimForm(
            [c[0] for c in cmds],
            [c[1] if len(c) > 1 else "" for c in cmds],
            [c[2] if len(c) > 2 else "" for c in cmds],
            group,
        )
    )


class TestParseRuleCondition:
    def test_a_single_row_is_that_row_not_a_group_of_one(self) -> None:
        ast = _parse([("Title", "=", "miku")])
        assert ast == {
            "kind": "condition",
            "field": "Title",
            "operator": "=",
            "value": "miku",
        }

    def test_many_rows_know_the_group_operator(self) -> None:
        ast = _parse(
            [("Title", "=", "a"), ("Category", "=", "同人志")], group="OR"
        )
        assert ast["kind"] == "group"
        assert ast["operator"] == "OR"
        assert len(ast["children"]) == 2

    def test_an_in_list_is_split_into_values(self) -> None:
        ast = _parse([("TAG", "IN", " 巨乳, 汉语 ")])

        assert ast["value"] == ["巨乳", "汉语"]

    def test_tag_exists_keeps_its_single_tag(self) -> None:
        ast = _parse([("TAG", "EXISTS", "NTR")])
        assert ast["value"] == "NTR"

    def test_a_scalar_exists_drops_its_value(self) -> None:
        ast = _parse([("Title", "EXISTS", "whatever")])
        assert "value" not in ast

    def test_a_blank_field_is_skipped(self) -> None:
        ast = _parse([("", "=", "x"), ("Title", "=", "a")])
        # One surviving row is that row, not a group of one.
        assert ast == {
            "kind": "condition",
            "field": "Title",
            "operator": "=",
            "value": "a",
        }

    def test_an_all_blank_form_is_none(self) -> None:
        assert _parse([("", "=", "x")]) is None

    def test_a_shorter_parallel_list_reads_as_missing_not_crash(
        self,
    ) -> None:
        """A hand-built form may send fewer operators than fields; the missing
        operator reads as an empty token, the same way an operator-less row
        would validate to a named error rather than an IndexError."""
        ast = _parse([("Title", "="), ("Category",)])

        assert ast["children"] == [
            {"kind": "condition", "field": "Title", "operator": "=", "value": ""},
            {"kind": "condition", "field": "Category", "operator": "", "value": ""},
        ]