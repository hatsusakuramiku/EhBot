import pytest

from app.auto_approval.rules import (
    RuleValidationError,
    editor_rows,
    evaluate_rule,
    render_rule_dsl,
    validate_rule_ast,
)


def test_tag_conditions_use_raw_and_chinese_tags() -> None:
    rule = validate_rule_ast(
        {
            "kind": "group",
            "operator": "AND",
            "children": [
                {
                    "kind": "condition",
                    "field": "TAG",
                    "operator": "HAS_ALL",
                    "value": ["language:chinese", "巨乳"],
                }
            ],
        }
    )

    result = evaluate_rule(
        rule,
        {
            "TagsRaw": "language:chinese, female:big breasts",
            "Tags": "汉语, 巨乳",
        },
    )

    assert result.matched
    assert result.conditions[0]["matched"] is True


def test_rule_supports_nested_boolean_numeric_and_like_conditions() -> None:
    rule = validate_rule_ast(
        {
            "kind": "group",
            "operator": "AND",
            "children": [
                {
                    "kind": "group",
                    "operator": "OR",
                    "children": [
                        {
                            "kind": "condition",
                            "field": "LanguageRaw",
                            "operator": "=",
                            "value": "chinese",
                        },
                        {
                            "kind": "condition",
                            "field": "Language",
                            "operator": "=",
                            "value": "中文",
                        },
                    ],
                },
                {
                    "kind": "condition",
                    "field": "Rating",
                    "operator": ">=",
                    "value": 4.0,
                },
                {
                    "kind": "condition",
                    "field": "Title",
                    "operator": "LIKE",
                    "value": "%sample%",
                },
            ],
        }
    )

    result = evaluate_rule(
        rule,
        {"LanguageRaw": "chinese", "Rating": "4.5", "Title": "A Sample"},
    )

    assert result.matched
    assert "{Rating} >= 4.0" in render_rule_dsl(rule)


def test_regex_rule_searches_field_and_renders_regex_dsl() -> None:
    rule = validate_rule_ast(
        {"kind": "regex", "field": "Title", "pattern": "(futa|chinpo)"}
    )

    assert render_rule_dsl(rule) == 'Regex({Title}, "(futa|chinpo)")'
    assert evaluate_rule(rule, {"Title": "futanari story by author"}).matched
    assert not evaluate_rule(rule, {"Title": "Futanari cased-differently"}).matched
    assert not evaluate_rule(rule, {"Title": "plain story"}).matched
    assert not evaluate_rule(rule, {"Title": ""}).matched


def test_regex_rule_honours_inline_flags_and_anchoring() -> None:
    casefold = validate_rule_ast(
        {"kind": "regex", "field": "Title", "pattern": "(?i)futa"}
    )
    assert evaluate_rule(casefold, {"Title": "FUTANARI"}).matched

    anchored = validate_rule_ast(
        {"kind": "regex", "field": "Artist", "pattern": "^tendou$"}
    )
    assert evaluate_rule(anchored, {"Artist": "tendou"}).matched
    assert not evaluate_rule(anchored, {"Artist": "tendourin"}).matched


def test_regex_rule_rejects_bad_pattern_field_or_empty_pattern() -> None:
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "regex", "field": "Title", "pattern": "(unclosed"}
        )
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "regex", "field": "Rating", "pattern": "^5$"}
        )
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "regex", "field": "Title", "pattern": "   "}
        )


def test_rule_rejects_unknown_fields_and_invalid_operator_types() -> None:
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {
                "kind": "condition",
                "field": "__import__",
                "operator": "=",
                "value": "x",
            }
        )
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {
                "kind": "condition",
                "field": "TAG",
                "operator": "HAS_ALL",
                "value": "not-a-list",
            }
        )


def test_editor_rows_round_trips_a_flat_rule() -> None:
    """A saved rule reads back as the form rows that produced it.

    This is what makes 编辑 possible at all: without the inverse, a stored rule
    could only be enabled, disabled or replaced.
    """
    ast = validate_rule_ast(
        {
            "kind": "group",
            "operator": "OR",
            "children": [
                {
                    "kind": "condition",
                    "field": "Category",
                    "operator": "=",
                    "value": "同人志",
                },
                {
                    "kind": "condition",
                    "field": "TAG",
                    "operator": "HAS_ANY",
                    "value": ["巨乳", "汉语"],
                },
                {"kind": "regex", "field": "Title", "pattern": "^\\[.+\\]"},
            ],
        }
    )

    decomposed = editor_rows(ast)
    assert decomposed is not None
    operator, rows = decomposed
    assert operator == "OR"
    assert rows[0] == {
        "kind": "condition",
        "field": "Category",
        "operator": "=",
        "value": "同人志",
    }
    # 「, 」 is what the parser splits a list row on, so a list rule survives an
    # edit that does not touch it.
    assert rows[1]["value"] == "巨乳, 汉语"
    assert rows[2] == {
        "kind": "regex",
        "field": "Title",
        "operator": "",
        "value": "^\\[.+\\]",
    }


def test_editor_rows_reads_a_single_node_rule_as_one_row() -> None:
    """One row saves as its own node, so it has to read back as one row."""
    ast = validate_rule_ast(
        {
            "kind": "condition",
            "field": "Rating",
            "operator": ">",
            "value": 4.5,
        }
    )

    decomposed = editor_rows(ast)
    assert decomposed is not None
    operator, rows = decomposed
    assert operator == "AND"
    assert len(rows) == 1
    assert rows[0]["value"] == "4.5"


def test_editor_rows_refuses_a_nested_group() -> None:
    """Refusing beats flattening.

    The flat editor emits one level, so a nested rule can only have been written
    into the database by hand. Flattening it would change what it matches while
    keeping its name, and the operator pressing 保存 would have no way to know.
    """
    ast = validate_rule_ast(
        {
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
    )

    assert editor_rows(ast) is None
