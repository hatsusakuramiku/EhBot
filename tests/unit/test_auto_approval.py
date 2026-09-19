import pytest

from app.auto_approval.rules import (
    RuleValidationError,
    editor_rows,
    evaluate_rule,
    normalize_operator,
    render_rule_dsl,
    validate_rule_ast,
)


# ------------------------------------------------------------ operators, roles


def test_normalize_operator_accepts_any_case_and_spacing() -> None:
    for raw in ("NOT_LIKE", "not like", "NOT LIKE", "Not Like", "not_like"):
        assert normalize_operator(raw) == "NOT_LIKE"
    for raw in ("not in", "NOT_IN", "Not In"):
        assert normalize_operator(raw) == "NOT_IN"
    for raw in ("not exists", "NOT_EXISTS", "Not Exists"):
        assert normalize_operator(raw) == "NOT_EXISTS"
    for raw in (">=", "> =", "= "):
        assert normalize_operator(raw) == raw.strip()


def test_operators_for_each_field_role() -> None:
    # Text: equality, LIKE, list and existence, but no range comparison.
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "condition", "field": "Title", "operator": ">", "value": "5"}
        )
    # Numeric: equality and range allowed.
    validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": ">", "value": "4"}
    )
    # Collection: no equality, no range.
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "condition", "field": "TAG", "operator": "=", "value": "a"}
        )
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "condition", "field": "TAG", "operator": ">", "value": "a"}
        )


def test_kind_regex_is_refused_and_unknown_operator_and_field() -> None:
    with pytest.raises(RuleValidationError):
        validate_rule_ast({"kind": "regex", "field": "Title", "pattern": "x"})
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "condition", "field": "Title", "operator": "HAS_ALL", "value": "a"}
        )
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "condition", "field": "__import__", "operator": "=", "value": "a"}
        )


def test_scalar_exists_rejects_a_value() -> None:
    with pytest.raises(RuleValidationError):
        validate_rule_ast(
            {"kind": "condition", "field": "Title", "operator": "EXISTS", "value": "x"}
        )


def test_tag_exists_requires_exactly_one_tag() -> None:
    for value in ("", None):
        with pytest.raises(RuleValidationError):
            validate_rule_ast(
                {"kind": "condition", "field": "TAG", "operator": "EXISTS", "value": value}
            )


# ------------------------------------------------------------ evaluation


def test_title_like_and_tag_not_exists_match_the_example_rule() -> None:
    """The rule from the request: no miku in the title AND no NTR tag."""
    rule = validate_rule_ast(
        {
            "kind": "group",
            "operator": "AND",
            "children": [
                {
                    "kind": "condition",
                    "field": "Title",
                    "operator": "NOT_LIKE",
                    "value": "%miku%",
                },
                {
                    "kind": "condition",
                    "field": "TAG",
                    "operator": "NOT_EXISTS",
                    "value": "NTR",
                },
            ],
        }
    )
    assert evaluate_rule(
        rule, {"Title": "A plain story", "TagsRaw": "female:x, 巨乳"}
    ).matched
    assert not evaluate_rule(
        rule, {"Title": "Miku 3D", "TagsRaw": "female:x"}
    ).matched
    assert not evaluate_rule(
        rule, {"Title": "A plain story", "TagsRaw": "NTR, male:x"}
    ).matched


def test_like_wildcards_percent_and_underscore() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "%miku%"}
    )
    assert evaluate_rule(rule, {"Title": "Hatsune Miku collection"}).matched
    assert not evaluate_rule(rule, {"Title": "plain story"}).matched
    assert not evaluate_rule(rule, {"Title": ""}).matched

    single = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "mi_ku"}
    )
    assert evaluate_rule(single, {"Title": "minku"}).matched
    # `_` matches exactly one character, so a 4-letter title is too short.
    assert not evaluate_rule(single, {"Title": "miku"}).matched


def test_like_literal_percent_and_underscore_are_escaped_with_brackets() -> None:
    # `[%]` makes the % literal, so the same pattern no longer matches any run.
    literal_percent = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "100[%]st"}
    )
    assert evaluate_rule(literal_percent, {"Title": "100%st"}).matched
    assert not evaluate_rule(literal_percent, {"Title": "100mst"}).matched

    literal_underscore = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "a[_]b"}
    )
    assert evaluate_rule(literal_underscore, {"Title": "a_b"}).matched
    assert not evaluate_rule(literal_underscore, {"Title": "axb"}).matched


def test_not_like() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "NOT_LIKE", "value": "%untagged%"}
    )
    assert evaluate_rule(rule, {"Title": "plain story"}).matched
    assert not evaluate_rule(rule, {"Title": "an untagged page"}).matched


def test_in_and_not_in_lists() -> None:
    rule = validate_rule_ast(
        {
            "kind": "condition",
            "field": "TAG",
            "operator": "IN",
            "value": ["巨乳", "汉语"],
        }
    )
    assert evaluate_rule(rule, {"Tags": "汉语, 纯爱"}).matched
    assert not evaluate_rule(rule, {"Tags": "纯爱, MALE:X"}).matched

    excluded = validate_rule_ast(
        {
            "kind": "condition",
            "field": "TAG",
            "operator": "NOT_IN",
            "value": ["NTR", "scat"],
        }
    )
    assert evaluate_rule(excluded, {"TagsRaw": "female:x"}).matched
    assert not evaluate_rule(excluded, {"TagsRaw": "NTR, male:x"}).matched


def test_in_on_a_text_field_matches_any_value() -> None:
    rule = validate_rule_ast(
        {
            "kind": "condition",
            "field": "Language",
            "operator": "IN",
            "value": ["中文", "日本語"],
        }
    )
    assert evaluate_rule(rule, {"Language": "中文"}).matched
    assert not evaluate_rule(rule, {"Language": "English"}).matched


def test_tag_exists_and_not_exists_are_set_membership() -> None:
    present = validate_rule_ast(
        {"kind": "condition", "field": "TAG", "operator": "EXISTS", "value": "NTR"}
    )
    assert evaluate_rule(present, {"TagsRaw": "NTR, male:x"}).matched
    assert not evaluate_rule(present, {"Tags": "汉语"}).matched

    absent = validate_rule_ast(
        {"kind": "condition", "field": "TAG", "operator": "NOT_EXISTS", "value": "NTR"}
    )
    assert evaluate_rule(absent, {"TagsRaw": "male:x, 巨乳"}).matched
    assert not evaluate_rule(absent, {"Tags": "汉语, NTR"}).matched


def test_tag_like_matches_any_single_tag_name() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "TAG", "operator": "LIKE", "value": "%female%"}
    )
    assert evaluate_rule(rule, {"TagsRaw": "male:x, female:big breasts"}).matched
    assert not evaluate_rule(rule, {"Tags": "汉语, 纯爱"}).matched


def test_scalar_exists_is_field_emptiness() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "EXISTS"}
    )
    assert evaluate_rule(rule, {"Title": "something"}).matched
    assert not evaluate_rule(rule, {"Title": ""}).matched
    assert not evaluate_rule(rule, {}).matched


def test_numeric_compare_and_numeric_equality() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": ">=", "value": "4"}
    )
    assert evaluate_rule(rule, {"Rating": "4.5"}).matched
    assert not evaluate_rule(rule, {"Rating": "3.9"}).matched

    equal = validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": "=", "value": "4"}
    )
    # A stored `4.0` compares numerically equal to `4` -- the fix this rewrite
    # brought in, where string equality would have missed it.
    assert evaluate_rule(equal, {"Rating": "4.0"}).matched
    assert not evaluate_rule(equal, {"Rating": "4.5"}).matched


def test_numeric_in_list() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": "IN", "value": ["4", "5"]}
    )
    assert evaluate_rule(rule, {"Rating": "5.0"}).matched
    assert not evaluate_rule(rule, {"Rating": "4.5"}).matched


def test_matching_is_case_insensitive_by_default_and_flips_on_the_toggle() -> None:
    rule = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "%Miku%"}
    )
    assert evaluate_rule(rule, {"Title": "miku chan"}).matched
    assert not evaluate_rule(rule, {"Title": "miku chan"}, case_sensitive=True).matched
    assert evaluate_rule(rule, {"Title": "Miku chan"}, case_sensitive=True).matched


# ------------------------------------------------------------ DSL rendering


def test_render_rule_dsl_forms() -> None:
    assert (
        render_rule_dsl(
            validate_rule_ast(
                {"kind": "condition", "field": "Title", "operator": "NOT_LIKE", "value": "%miku%"}
            )
        )
        == '{Title} NOT LIKE "%miku%"'
    )
    assert (
        render_rule_dsl(
            validate_rule_ast(
                {"kind": "condition", "field": "TAG", "operator": "NOT_EXISTS", "value": "NTR"}
            )
        )
        == '{TAG} NOT EXISTS("NTR")'
    )
    assert (
        render_rule_dsl(
            validate_rule_ast(
                {"kind": "condition", "field": "TAG", "operator": "IN", "value": ["a", "b"]}
            )
        )
        == '{TAG} IN ("a", "b")'
    )
    assert (
        render_rule_dsl(
            validate_rule_ast({"kind": "condition", "field": "Title", "operator": "EXISTS"})
        )
        == "{Title} EXISTS"
    )
    assert (
        render_rule_dsl(
            validate_rule_ast(
                {"kind": "condition", "field": "Rating", "operator": ">=", "value": "4"}
            )
        )
        == "{Rating} >= 4.0"
    )


def test_render_rule_dsl_group() -> None:
    rule = validate_rule_ast(
        {
            "kind": "group",
            "operator": "AND",
            "children": [
                {"kind": "condition", "field": "Title", "operator": "=", "value": "a"},
                {"kind": "condition", "field": "Title", "operator": "=", "value": "b"},
            ],
        }
    )
    assert render_rule_dsl(rule) == '({Title} = "a" AND {Title} = "b")'


# ------------------------------------------------------------ editor round trip


def test_editor_rows_round_trips_a_flat_rule() -> None:
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
                    "operator": "IN",
                    "value": ["巨乳", "汉语"],
                },
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
    # 「, 」 is what the parser splits a list row on, so an IN rule survives an
    # edit that does not touch it.
    assert rows[1]["value"] == "巨乳, 汉语"


def test_editor_rows_reads_a_single_node_rule_as_one_row() -> None:
    ast = validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": ">", "value": 4.5}
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


def test_editor_rows_renders_exists_and_in_values_for_the_form() -> None:
    """The inverse has to put TAG EXISTS' tag and IN's list back into the input."""
    tag_exists = validate_rule_ast(
        {"kind": "condition", "field": "TAG", "operator": "NOT_EXISTS", "value": "NTR"}
    )
    _, rows = editor_rows(tag_exists)
    assert rows[0]["value"] == "NTR"

    scalar_exists = validate_rule_ast(
        {"kind": "condition", "field": "Title", "operator": "EXISTS"}
    )
    _, rows = editor_rows(scalar_exists)
    assert rows[0]["value"] == ""

    tag_in = validate_rule_ast(
        {"kind": "condition", "field": "TAG", "operator": "IN", "value": ["a", "b"]}
    )
    _, rows = editor_rows(tag_in)
    assert rows[0]["value"] == "a, b"


def test_a_whole_number_comparison_reads_back_without_a_fraction() -> None:
    ast = validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": ">", "value": "4"}
    )
    assert ast["value"] == 4.0

    decomposed = editor_rows(ast)
    assert decomposed is not None
    _, rows = decomposed
    assert rows[0]["value"] == "4"


def test_a_fractional_comparison_keeps_its_fraction() -> None:
    ast = validate_rule_ast(
        {"kind": "condition", "field": "Rating", "operator": ">=", "value": "4.5"}
    )
    decomposed = editor_rows(ast)
    assert decomposed is not None
    _, rows = decomposed
    assert rows[0]["value"] == "4.5"