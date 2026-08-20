"""Unit tests for the metadata-based candidate rules."""

from dataclasses import replace

from app.candidates.models import TelegramSourceConfig
from app.candidates.rules import evaluate_metadata_rules


def _source(**overrides) -> TelegramSourceConfig:
    base = TelegramSourceConfig(
        source_id=1,
        source_type="CHANNEL",
        chat_id=-100,
        display_name="Fixture",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    return replace(base, **overrides)


def test_no_rules_accepts_everything() -> None:
    decision = evaluate_metadata_rules(_source(), {"Title": "anything"})
    assert decision.result == "ACCEPT"


def test_required_tag_missing_ignores() -> None:
    source = _source(required_tags=("language:chinese",))
    decision = evaluate_metadata_rules(
        source, {"Tags": "female:big_breasts"}
    )
    assert decision.result == "IGNORE"
    assert "language:chinese" in decision.reason


def test_required_tag_present_accepts() -> None:
    source = _source(required_tags=("language:chinese",))
    decision = evaluate_metadata_rules(source, {"Tags": "language:chinese"})
    assert decision.result == "ACCEPT"


def test_required_tag_can_match_original_tag_row() -> None:
    source = _source(required_tags=("female:big breasts",))
    decision = evaluate_metadata_rules(
        source,
        {"TagsRaw": "female:big breasts", "Tags": "巨乳"},
    )
    assert decision.result == "ACCEPT"


def test_forbidden_tag_ignores() -> None:
    source = _source(forbidden_tags=("male:only",))
    decision = evaluate_metadata_rules(
        source, {"Tags": "male:only, language:chinese"}
    )
    assert decision.result == "IGNORE"
    assert "male:only" in decision.reason


def test_allowed_language_missing_needs_info() -> None:
    source = _source(allowed_languages=("chinese",))
    decision = evaluate_metadata_rules(source, {})
    assert decision.result == "NEEDS_INFO"


def test_allowed_language_mismatch_ignores() -> None:
    source = _source(allowed_languages=("chinese",))
    decision = evaluate_metadata_rules(source, {"Language": "english"})
    assert decision.result == "IGNORE"
    assert "english" in decision.reason


def test_allowed_language_match_accepts() -> None:
    source = _source(allowed_languages=("chinese",))
    decision = evaluate_metadata_rules(source, {"Language": "chinese"})
    assert decision.result == "ACCEPT"


def test_allowed_category_case_insensitive() -> None:
    source = _source(allowed_categories=("doujinshi",))
    decision = evaluate_metadata_rules(source, {"Category": "Doujinshi"})
    assert decision.result == "ACCEPT"


def test_min_rating_below_threshold_ignores() -> None:
    source = _source(min_rating=4.0)
    decision = evaluate_metadata_rules(source, {"Rating": "3.5"})
    assert decision.result == "IGNORE"
    assert "3.5" in decision.reason


def test_min_rating_meets_threshold_accepts() -> None:
    source = _source(min_rating=4.0)
    decision = evaluate_metadata_rules(source, {"Rating": "4.0"})
    assert decision.result == "ACCEPT"


def test_min_rating_missing_needs_info() -> None:
    source = _source(min_rating=4.0)
    decision = evaluate_metadata_rules(source, {})
    assert decision.result == "NEEDS_INFO"


def test_combined_required_and_forbidden() -> None:
    source = _source(
        required_tags=("language:chinese",),
        forbidden_tags=("male:only",),
    )
    decision = evaluate_metadata_rules(
        source, {"Tags": "language:chinese, male:only"}
    )
    assert decision.result == "IGNORE"
    assert "male:only" in decision.reason


def test_tags_list_parses_newline_separated() -> None:
    source = _source(required_tags=("language:chinese",))
    decision = evaluate_metadata_rules(
        source, {"Tags": "female:big_breasts\nlanguage:chinese"}
    )
    assert decision.result == "ACCEPT"
