import pytest

from app.auto_approval.rules import (
    RuleValidationError,
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
