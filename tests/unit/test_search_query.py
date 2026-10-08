"""Unit tests for the R61 search grammar: `$…$` blocks, free text, degradation."""

from __future__ import annotations

import pytest

from app.search.query import (
    PAGE_CANDIDATES,
    PAGE_DOWNLOADED,
    Filter,
    candidate_search_sql,
    downloaded_search_sql,
    has_wildcard,
    like_pattern,
    parse_query,
)


# ------------------------------------------------------------------- grammar


def test_free_text_splits_into_anded_words() -> None:
    parsed = parse_query("ntr cosplay")

    # R61 turns the old whole-string substring into per-word AND, so 「ntr
    # cosplay」 means both rather than the literal phrase.
    assert [(term.value, term.exclude) for term in parsed.terms] == [
        ("ntr", False),
        ("cosplay", False),
    ]
    assert parsed.filters == ()


def test_a_block_holds_one_condition() -> None:
    parsed = parse_query("$tag:ntr$")

    (item,) = parsed.filters
    assert item.field == "TAG"
    assert item.value == "ntr"
    assert (item.exclude, item.fuzzy) == (False, False)
    assert parsed.terms == ()


def test_a_block_holds_several_space_separated_conditions() -> None:
    parsed = parse_query("$tag:ntr language:chinese$")

    assert [(item.field, item.value) for item in parsed.filters] == [
        ("TAG", "ntr"),
        ("Language", "chinese"),
    ]


def test_several_blocks_are_all_kept() -> None:
    parsed = parse_query("$tag:ntr$ $artist:sample$")

    assert [item.raw for item in parsed.filters] == ["tag:ntr", "artist:sample"]


def test_a_leading_dash_excludes_in_a_block_and_in_free_text() -> None:
    parsed = parse_query("$-tag:ntr$ -cosplay")

    assert parsed.filters[0].exclude is True
    assert [term.exclude for term in parsed.terms] == [True]


def test_a_quoted_value_keeps_its_spaces() -> None:
    parsed = parse_query('$tag:"sole female"$')

    (item,) = parsed.filters
    assert item.value == "sole female"
    # The raw slice keeps the quotes so a chip can find it again in the box.
    assert item.raw == 'tag:"sole female"'


def test_field_names_are_case_insensitive() -> None:
    parsed = parse_query("$TAG:ntr$")

    assert parsed.filters[0].field == "TAG"


def test_the_tag_alias_covers_tags_and_tagsraw() -> None:
    assert parse_query("$tags:ntr$").filters[0].field == "TAG"


# ------------------------------------------------------------------ wildcards


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", False),
        ("%ntr%", True),
        ("so_e", True),
        ("[%]", False),
        ("[_]", False),
    ],
)
def test_has_wildcard_looks_past_the_literal_escapes(value, expected) -> None:
    assert has_wildcard(value) is expected


def test_a_wildcard_makes_the_comparison_fuzzy() -> None:
    assert parse_query("$tag:%cosplay%$").filters[0].fuzzy is True
    assert parse_query("$tag:cosplay$").filters[0].fuzzy is False


def test_like_pattern_keeps_wildcards_and_escapes_the_literals() -> None:
    assert like_pattern("100%") == "100%"
    assert like_pattern("a_b") == "a_b"
    assert like_pattern("[%]") == "\\%"
    assert like_pattern("[_]") == "\\_"


def test_like_pattern_escapes_the_escape_and_a_bare_bracket() -> None:
    # A backslash is the ESCAPE character, so a literal one has to be doubled;
    # a bare `[` must not be read as the start of a literal group.
    assert like_pattern("a\\b") == "a\\\\b"
    assert like_pattern("[x]") == "\\[x]"


def test_free_text_wildcards_are_wildcards_too() -> None:
    parsed = parse_query("ntr%")

    assert parsed.terms[0].value == "ntr%"


# ---------------------------------------------------------------- degradation


def test_double_dollar_degrades_as_an_empty_block() -> None:
    parsed = parse_query("$$")

    assert parsed.active is False
    assert [(item.text, item.reason) for item in parsed.degraded] == [
        ("$$", "空的过滤块")
    ]


def test_nested_double_dollar_falls_back_to_the_global_search() -> None:
    parsed = parse_query("$$tag:ntr$$")

    # Rule 2: nothing here is a filter, the middle is searched for as text.
    assert parsed.filters == ()
    assert [term.value for term in parsed.terms] == ["tag:ntr"]
    assert parsed.degraded


def test_half_a_block_degrades_to_the_global_search() -> None:
    parsed = parse_query("$tag:bad")

    assert parsed.filters == ()
    assert [(item.text, item.reason) for item in parsed.degraded] == [
        ("$tag:bad", "过滤块没有闭合")
    ]
    assert [term.value for term in parsed.terms] == ["$tag:bad"]


def test_a_broken_block_does_not_take_its_neighbour_down() -> None:
    parsed = parse_query("$tag:ntr$ $tag:bad")

    # The operator's 分段退化: the readable block still filters.
    assert [item.value for item in parsed.filters] == ["ntr"]
    assert [item.text for item in parsed.degraded] == ["$tag:bad"]


def test_a_broken_block_drops_neither_half_on_the_floor() -> None:
    parsed = parse_query("$tag:ntr$ $坏$")

    # The proposal's own case (§2.4): the tag block keeps filtering, and the
    # unreadable block's words still go to the global search.
    assert [item.value for item in parsed.filters] == ["ntr"]
    assert [(item.text, item.reason) for item in parsed.degraded] == [
        ("$坏$", "无法识别的条件 坏")
    ]
    assert [term.value for term in parsed.terms] == ["坏"]


def test_an_unclosed_quote_is_reported_but_still_searched() -> None:
    parsed = parse_query('$tag:"unclosed')

    assert parsed.filters == ()
    assert parsed.degraded[0].reason == "过滤块没有闭合"


def test_an_empty_or_blank_query_is_not_an_error() -> None:
    for text in ("", "   ", "\t"):
        parsed = parse_query(text)

        assert parsed.active is False
        assert parsed.degraded == ()


# --------------------------------------------------------------------- fields


@pytest.mark.parametrize("field", ["pages", "rating", "filesize"])
def test_numeric_fields_are_not_searchable(field: str) -> None:
    parsed = parse_query(f"${field}:5$")

    # The operator decided numbers do not participate in filtering at all, so
    # there is no condition -- and the block says why instead of vanishing.
    assert parsed.filters == ()
    assert parsed.degraded[0].reason == f"不支持的字段 {field}"


def test_an_unknown_field_degrades() -> None:
    parsed = parse_query("$nonsense:x$")

    assert parsed.filters == ()
    assert parsed.degraded[0].reason == "不支持的字段 nonsense"


def test_status_is_candidate_only() -> None:
    assert parse_query("$status:待审核$", page=PAGE_CANDIDATES).filters[0].codes == (
        "PENDING_REVIEW",
    )
    parsed = parse_query("$status:待审核$", page=PAGE_DOWNLOADED)
    assert parsed.filters == ()
    assert parsed.degraded[0].reason == "status 只在候选页可用"


def test_source_is_downloaded_only() -> None:
    parsed = parse_query("$source:eh$", page=PAGE_DOWNLOADED)
    assert parsed.filters[0].field == "source"
    assert parsed.filters[0].codes == ("EH_TORRENT", "EXHENTAI")

    parsed = parse_query("$source:eh$", page=PAGE_CANDIDATES)
    assert parsed.filters == ()
    assert parsed.degraded[0].reason == "source 只在已下载页可用"


def test_an_unknown_status_value_degrades() -> None:
    parsed = parse_query("$status:nope$")

    assert parsed.filters == ()
    assert parsed.degraded[0].reason == "未知状态 nope"


# ---------------------------------------------------------------------- chips


def test_a_chip_distinguishes_whole_value_from_pattern() -> None:
    assert parse_query("$tag:ntr$").filters[0].chip == "标签 = ntr"
    assert parse_query("$tag:%ntr%$").filters[0].chip == "标签 ≈ %ntr%"
    assert parse_query("$-tag:ntr$").filters[0].chip == "不含 标签 = ntr"


def test_removing_a_chip_keeps_its_neighbours() -> None:
    parsed = parse_query("$tag:ntr tag:cosplay$")

    assert parsed.without("tag:ntr") == "$ tag:cosplay$"


def test_removing_the_last_chip_drops_the_block() -> None:
    parsed = parse_query("$tag:ntr$")

    # Leaving `$ $` behind would come back as a broken empty block next submit.
    assert parsed.without("tag:ntr") == ""


def test_removing_a_chip_keeps_surrounding_free_text() -> None:
    parsed = parse_query("hello $tag:ntr$ world")

    assert parsed.without("tag:ntr") == "hello world"


def test_the_payload_carries_the_filters_for_a_client() -> None:
    payload = parse_query("$tag:ntr$ -cosplay").to_payload()

    assert payload["filters"] == [
        {
            "field": "TAG",
            "label": "标签",
            "value": "ntr",
            "exclude": False,
            "fuzzy": False,
            "raw": "tag:ntr",
        }
    ]
    assert payload["terms"] == [{"value": "cosplay", "exclude": True}]


# -------------------------------------------------------------------- compile


def test_a_status_condition_compiles_to_a_status_clause() -> None:
    compiled = candidate_search_sql(parse_query("$status:待审核$"))

    assert compiled.clause == "(c.status IN (?))"
    assert compiled.params == ("PENDING_REVIEW",)


def test_excluding_a_status_wraps_it_in_not() -> None:
    compiled = candidate_search_sql(parse_query("$-status:待审核$"))

    assert compiled.clause == "(NOT (c.status IN (?)))"


def test_a_source_condition_binds_the_provider_column() -> None:
    compiled = downloaded_search_sql(
        parse_query("$source:eh$", page=PAGE_DOWNLOADED)
    )

    assert compiled.clause == "(dj.provider IN (?, ?))"
    assert compiled.params == ("EH_TORRENT", "EXHENTAI")


def test_a_tag_condition_generates_parameters_in_order() -> None:
    compiled = candidate_search_sql(parse_query("$tag:ntr$"))

    assert len(compiled.params) == 4
    assert compiled.params == ("Tags", "TagsRaw", "%,ntr,%", "%:ntr,%")


def test_an_empty_query_compiles_to_no_clause() -> None:
    compiled = candidate_search_sql(parse_query(""))

    assert compiled.clause == "1=1"
    assert compiled.params == ()


def test_a_whole_value_match_wraps_the_pattern_in_the_comma_fence() -> None:
    compiled = candidate_search_sql(parse_query("$tag:solo$"))

    # Fenced so 「solo」 cannot match the tail of a longer tag, and the second
    # pattern is the namespace-stripped spelling (`female:netorare`).
    assert compiled.params[-2:] == ("%,solo,%", "%:solo,%")
    assert "replace(replace(mv.field_value" in compiled.clause


def test_a_literal_wildcard_is_escaped_in_the_sql_parameter() -> None:
    compiled = candidate_search_sql(parse_query("$title:[%]$"))

    # `[%]` names the literal character, so the LIKE pattern carries `\\%`;
    # nothing the operator typed reaches the SQL text itself.
    assert compiled.params[-1] == "\\%"


def test_a_filter_object_is_immutable_and_labelled() -> None:
    item = Filter(
        field="Title", value="x", exclude=False, fuzzy=False, raw="title:x"
    )

    assert item.label == "标题"
    assert item.to_payload()["label"] == "标题"
