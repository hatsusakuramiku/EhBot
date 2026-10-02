"""The candidate-admission parse scheme: validation, reads, and the gate.

Pure functions over a `ParsedSourceMessage`, so there is no database here. What
is worth pinning is the *semantics* of the default: 「只解析含 Eh 链接的消息」
is not just a checkbox label, it is the answer `message_qualifies` gives for a
photo-only message, and every other test in the suite depends on knowing it.
"""

from __future__ import annotations

import pytest

from app.candidates.models import ParsedSourceMessage
from app.candidates.parse_rules import (
    ARCHIVE_FORMATS,
    DEFAULT_PARSE_RULES,
    ParseRulesError,
    default_parse_rules,
    dump_parse_rules,
    message_qualifies,
    parse_rules_view,
    title_forces_needs_info,
    validate_parse_rules,
)


def _message(
    *,
    ex_gid: int | None = None,
    attachments: tuple[dict, ...] = (),
    preview_urls: tuple[str, ...] = (),
    title: str | None = "作品",
) -> ParsedSourceMessage:
    return ParsedSourceMessage(
        is_edit=False,
        chat_id=-100,
        chat_title="Channel",
        message_id=1,
        sender_id=None,
        reply_to_message_id=None,
        media_group_id=None,
        message_text="",
        attachments=attachments,
        file_unique_id=None,
        message_date="2026-01-01T00:00:00+00:00",
        title=title,
        title_source="TELEGRAM" if title else None,
        title_confidence=0.9 if title else None,
        filter_result="ACCEPT",
        filter_reason="",
        ex_gid=ex_gid,
        preview_urls=preview_urls,
    )


def _archive(name: str = "book.zip") -> tuple[dict, ...]:
    return ({"type": "archive", "file_name": name},)


def _photo() -> tuple[dict, ...]:
    return ({"type": "photo", "file_name": ""},)


class TestDefaults:
    def test_the_shipped_scheme_is_gallery_links_only(self) -> None:
        assert DEFAULT_PARSE_RULES == {
            "require_gallery_link": True,
            "accept_photo": False,
            "accept_archive": False,
            "archive_formats": list(ARCHIVE_FORMATS),
            "accept_preview": False,
            "title_required": False,
        }

    def test_a_default_copy_is_not_shared(self) -> None:
        copy = default_parse_rules()
        copy["accept_photo"] = True
        assert default_parse_rules()["accept_photo"] is False


class TestValidation:
    def test_a_full_scheme_round_trips(self) -> None:
        rules = {
            "require_gallery_link": False,
            "accept_photo": True,
            "accept_archive": True,
            "archive_formats": ["zip", "cbz"],
            "accept_preview": True,
            "title_required": True,
        }
        assert validate_parse_rules(rules) == rules

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(ParseRulesError) as raised:
            validate_parse_rules({"nonsense": True})
        assert raised.value.code == "PARSE_RULES_UNKNOWN_KEY"

    def test_a_non_boolean_toggle_is_refused(self) -> None:
        with pytest.raises(ParseRulesError) as raised:
            validate_parse_rules({"accept_photo": "yes"})
        assert raised.value.code == "PARSE_RULES_INVALID_VALUE"

    def test_an_unknown_archive_format_is_refused(self) -> None:
        with pytest.raises(ParseRulesError) as raised:
            validate_parse_rules({"archive_formats": ["tar"]})
        assert raised.value.code == "PARSE_RULES_INVALID_FORMAT"

    def test_an_empty_format_list_is_refused(self) -> None:
        with pytest.raises(ParseRulesError):
            validate_parse_rules({"archive_formats": []})

    def test_a_missing_key_keeps_its_default(self) -> None:
        rules = validate_parse_rules({"accept_photo": True})
        assert rules["accept_photo"] is True
        assert rules["require_gallery_link"] is True

    def test_the_stored_form_is_read_back(self) -> None:
        stored = dump_parse_rules(
            validate_parse_rules({"accept_archive": True, "accept_photo": True})
        )
        assert parse_rules_view(stored)["accept_archive"] is True


class TestLenientReads:
    @pytest.mark.parametrize(
        "stored",
        [None, "", "{not json", "[1, 2]", '{"accept_photo": "maybe"}'],
    )
    def test_anything_unreadable_falls_back_to_the_default(
        self, stored: object
    ) -> None:
        assert parse_rules_view(stored) == DEFAULT_PARSE_RULES  # type: ignore[arg-type]

    def test_an_unknown_format_is_dropped_rather_than_fatal(self) -> None:
        rules = parse_rules_view('{"archive_formats": ["zip", "tar"]}')
        assert rules["archive_formats"] == ["zip"]


class TestGate:
    def test_a_gallery_link_always_qualifies(self) -> None:
        ok, reason = message_qualifies(DEFAULT_PARSE_RULES, _message(ex_gid=123))
        assert ok is True
        assert "画廊链接" in reason

    def test_the_default_rejects_a_photo_only_message(self) -> None:
        ok, reason = message_qualifies(DEFAULT_PARSE_RULES, _message(attachments=_photo()))
        assert ok is False
        assert "Eh" in reason or "画廊链接" in reason

    def test_the_default_rejects_an_archive_only_message(self) -> None:
        ok, _ = message_qualifies(DEFAULT_PARSE_RULES, _message(attachments=_archive()))
        assert ok is False

    def test_the_default_rejects_a_preview_only_message(self) -> None:
        ok, _ = message_qualifies(
            DEFAULT_PARSE_RULES, _message(preview_urls=("https://telegra.ph/x",))
        )
        assert ok is False

    def test_an_archive_is_accepted_when_the_operator_allows_it(self) -> None:
        rules = dict(DEFAULT_PARSE_RULES, require_gallery_link=False, accept_archive=True)
        ok, reason = message_qualifies(rules, _message(attachments=_archive("book.rar")))
        assert ok is True
        assert "压缩包" in reason

    def test_a_disallowed_format_is_refused_even_when_archives_are_allowed(self) -> None:
        rules = dict(
            DEFAULT_PARSE_RULES,
            require_gallery_link=False,
            accept_archive=True,
            archive_formats=["zip"],
        )
        ok, reason = message_qualifies(rules, _message(attachments=_archive("book.rar")))
        assert ok is False
        assert "RAR" in reason

    def test_a_photo_is_accepted_when_the_operator_allows_it(self) -> None:
        rules = dict(DEFAULT_PARSE_RULES, require_gallery_link=False, accept_photo=True)
        ok, _ = message_qualifies(rules, _message(attachments=_photo()))
        assert ok is True

    def test_a_preview_page_is_accepted_when_the_operator_allows_it(self) -> None:
        rules = dict(DEFAULT_PARSE_RULES, require_gallery_link=False, accept_preview=True)
        ok, _ = message_qualifies(
            rules, _message(preview_urls=("https://telegra.ph/x",))
        )
        assert ok is True

    def test_require_gallery_link_wins_over_the_other_toggles(self) -> None:
        """「只解析含 Eh 链接的消息」 has to mean it."""
        rules = dict(
            DEFAULT_PARSE_RULES,
            require_gallery_link=True,
            accept_photo=True,
            accept_archive=True,
        )
        ok, _ = message_qualifies(rules, _message(attachments=_archive()))
        assert ok is False

    def test_title_required_overrides_the_source_decision(self) -> None:
        rules = dict(DEFAULT_PARSE_RULES)
        assert title_forces_needs_info(rules, _message(title=None)) is False
        rules["title_required"] = True
        assert title_forces_needs_info(rules, _message(title=None)) is True
        assert title_forces_needs_info(rules, _message(title="有标题")) is False
