from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.review.models import METADATA_FIELDS, RAW_METADATA_FIELDS


#: A scalar numeric field compares numerically: `{Rating} = 4` matches a stored
#: `4.0`, and `>` / `<` only make sense here.
NUMERIC_FIELDS = frozenset({"Rating", "Pages"})

#: The collection pseudo-field. It matches against the tag set (both the Chinese
#: `Tags` and the raw `TagsRaw`, split and de-duplicated) rather than one stored
#: value, so its operators describe set membership, not string comparison.
COLLECTION_FIELD = "TAG"

#: Every field an instruction may name. `TAG` is not real metadata -- it is
#: offered beside the real fields because the editor shares one dropdown.
ALLOWED_FIELDS = frozenset(
    (*METADATA_FIELDS, *RAW_METADATA_FIELDS, "FileSize", "Web", "TAG")
)

TEXT_FIELDS = frozenset(ALLOWED_FIELDS - NUMERIC_FIELDS - {COLLECTION_FIELD})

#: Operators, canonical tokens. Two-word SQL spellings (`NOT LIKE`) are stored
#: with an underscore and rendered back spaced; both are accepted on input,
#: any case, via `normalize_operator`.
EQ_OPS = frozenset({"=", "<>"})
NUMERIC_COMPARE_OPS = frozenset({">", ">=", "<", "<="})
LIKE_OPS = frozenset({"LIKE", "NOT_LIKE"})
IN_OPS = frozenset({"IN", "NOT_IN"})
EXISTENCE_OPS = frozenset({"EXISTS", "NOT_EXISTS"})

TEXT_OPERATORS = frozenset(EQ_OPS | LIKE_OPS | IN_OPS | EXISTENCE_OPS)
NUMERIC_OPERATORS = frozenset(
    EQ_OPS | NUMERIC_COMPARE_OPS | IN_OPS | EXISTENCE_OPS
)
COLLECTION_OPERATORS = frozenset(EXISTENCE_OPS | IN_OPS | LIKE_OPS)
ALL_OPERATORS = frozenset(
    TEXT_OPERATORS | NUMERIC_OPERATORS | COLLECTION_OPERATORS
)

#: Token -> the SQL-style pairing an operator reads as. `NOT LIKE` reads better
#: with the space; the token keeps the underscore so it parses in one unit.
_OPERATOR_RENDER = {
    "NOT_LIKE": "NOT LIKE",
    "NOT_IN": "NOT IN",
    "NOT_EXISTS": "NOT EXISTS",
}
#: The converse, plus forgiveness for the underscore spelling and any case.
_SPACED_TO_TOKEN = {_OPERATOR_RENDER[k]: k for k in _OPERATOR_RENDER}


class RuleValidationError(ValueError):
    """Raised when an automatic-approval rule AST is outside the DSL."""


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    matched: bool
    conditions: tuple[dict[str, Any], ...]


def normalize_operator(raw: Any) -> str:
    """One operator string in canonical form, whatever it was typed as.

    Case- and spacing-insensitive, so `not like`, `NOT_LIKE`, `Not Like` and
    `NOT LIKE` are all the same operator. Corresponds to SQL's tolerance of
    spelling; only the canonical token is persisted.
    """
    spaced = " ".join(str(raw or "").split()).upper()
    return _SPACED_TO_TOKEN.get(spaced, spaced)


def _field_role(field: str) -> str:
    if field in NUMERIC_FIELDS:
        return "numeric"
    if field == COLLECTION_FIELD:
        return "collection"
    return "text"


def _role_operators(role: str) -> frozenset[str]:
    if role == "numeric":
        return NUMERIC_OPERATORS
    if role == "collection":
        return COLLECTION_OPERATORS
    return TEXT_OPERATORS


def _clean_list(raw_value: Any) -> list[str]:
    """A list (or a scalar), reduced to non-empty trimmed strings.

    A `None` member is dropped, not rendered as the four-letter string `"None"`:
    a TAG EXISTS rule built from a missing input is an empty rule, not one that
    demands a tag literally named `None`.
    """
    items = raw_value if isinstance(raw_value, list) else [raw_value]
    return [
        str(item).strip() for item in items if item is not None and str(item).strip()
    ]


def validate_rule_ast(value: object) -> dict[str, Any]:
    """Validate and normalize a persisted automatic-approval AST."""
    if not isinstance(value, dict):
        raise RuleValidationError("规则必须是对象")
    kind = str(value.get("kind") or "").lower()
    if kind == "regex":
        raise RuleValidationError("正则匹配已移除，请改用 LIKE")
    if kind == "group":
        operator = normalize_operator(value.get("operator") or "")
        children = value.get("children")
        if operator not in {"AND", "OR"}:
            raise RuleValidationError("条件组运算符必须是 AND 或 OR")
        if not isinstance(children, list) or not children:
            raise RuleValidationError("条件组至少需要一个条件")
        return {
            "kind": "group",
            "operator": operator,
            "children": [validate_rule_ast(child) for child in children],
        }
    if kind != "condition":
        raise RuleValidationError("条件类型无效")

    field = str(value.get("field") or "")
    if field not in ALLOWED_FIELDS:
        raise RuleValidationError(f"不支持字段 {field}")
    operator = normalize_operator(value.get("operator") or "")
    role = _field_role(field)
    if operator not in _role_operators(role):
        raise RuleValidationError(f"字段 {field} 不支持运算符 {operator}")

    return {
        "kind": "condition",
        "field": field,
        "operator": operator,
        **_condition_value(operator, role, value),
    }


def _condition_value(operator: str, role: str, value: dict) -> dict[str, Any]:
    """The normalized `value` payload for one leaf, or raise."""
    if operator in EXISTENCE_OPS:
        if role == "collection":
            items = _clean_list(value.get("value"))
            if len(items) != 1:
                raise RuleValidationError("标签的 EXISTS / NOT_EXISTS 需要恰好一个标签")
            return {"value": items[0]}
        raw = value.get("value")
        if raw not in (None, "") and _clean_list(raw):
            raise RuleValidationError("普通字段的 EXISTS / NOT_EXISTS 不需要值")
        return {}

    if operator in IN_OPS:
        candidates = _clean_list(value.get("value"))
        if not candidates:
            raise RuleValidationError(f"{operator} 需要至少一个值")
        if role == "numeric":
            try:
                [(float(item), item) for item in candidates]
            except ValueError as exc:
                raise RuleValidationError(f"{operator} 的数值含有非数字") from exc
        return {"value": candidates}

    if "value" not in value or value["value"] is None:
        raise RuleValidationError("比较条件需要值")

    raw = value["value"]
    if operator in NUMERIC_COMPARE_OPS:
        try:
            return {"value": float(raw)}
        except (TypeError, ValueError) as exc:
            raise RuleValidationError("数值比较条件需要数字") from exc
    if role == "numeric":
        try:
            float(raw)
        except (TypeError, ValueError) as exc:
            raise RuleValidationError("数值字段的比较条件需要数字") from exc
    normalized = str(raw).strip()
    if not normalized:
        raise RuleValidationError("比较条件值不能为空")
    return {"value": normalized}


def render_rule_dsl(ast: dict[str, Any]) -> str:
    """Render a validated AST as readable, non-executable DSL text."""
    if ast["kind"] == "group":
        children = [render_rule_dsl(child) for child in ast["children"]]
        return "(" + f" {ast['operator']} ".join(children) + ")"

    field = "{TAG}" if ast["field"] == COLLECTION_FIELD else "{" + ast["field"] + "}"
    operator = _OPERATOR_RENDER.get(ast["operator"], ast["operator"])
    value = ast.get("value")
    if ast["operator"] in EXISTENCE_OPS:
        if ast["field"] == COLLECTION_FIELD:
            return f"{field} {operator}({json.dumps(value, ensure_ascii=False)})"
        return f"{field} {operator}"
    if isinstance(value, list):
        rendered = "(" + ", ".join(
            json.dumps(item, ensure_ascii=False) for item in value
        ) + ")"
    elif isinstance(value, str):
        rendered = json.dumps(value, ensure_ascii=False)
    else:
        rendered = str(value)
    return f"{field} {operator} {rendered}"


def editor_rows(ast: dict[str, Any]) -> tuple[str, tuple[dict[str, str], ...]] | None:
    """Decompose a stored AST back into the editor's flat rows, or refuse.

    The inverse of `_parse_rule_condition` in `app/web/routes/auto_approval.py`.
    Returns None for a group nested inside a group, which the flat editor cannot
    express -- refusing beats flattening it into a rule that means something
    else.
    """
    if ast.get("kind") == "group":
        operator = normalize_operator(ast.get("operator") or "")
        children = list(ast.get("children") or ())
        if any(child.get("kind") == "group" for child in children):
            return None
        return operator, tuple(_editor_row(child) for child in children)
    return "AND", (_editor_row(ast),)


def _editor_row(node: dict[str, Any]) -> dict[str, str]:
    """One AST leaf as the four strings its form row submits (kind is gone;
    the sub form carries only field / operator / value)."""
    field = str(node.get("field") or "")
    operator = str(node.get("operator") or "")
    value = node.get("value")
    if operator in EXISTENCE_OPS:
        if field == COLLECTION_FIELD:
            rendered = str(value or "")
        else:
            rendered = ""
    elif isinstance(value, list):
        rendered = ", ".join(str(item) for item in value)
    elif isinstance(value, float) and value.is_integer():
        rendered = str(int(value))
    elif value is None:
        rendered = ""
    else:
        rendered = str(value)
    return {"kind": "condition", "field": field, "operator": operator, "value": rendered}


def evaluate_rule(
    ast: dict[str, Any], metadata: dict[str, str], *, case_sensitive: bool = False
) -> RuleEvaluation:
    """Evaluate a validated AST against effective candidate metadata.

    `case_sensitive` applies to the whole rule (a rule-level toggle, default
    off): when False, text and tag comparisons case-fold on both sides.
    """
    conditions: list[dict[str, Any]] = []

    def evaluate(node: dict[str, Any]) -> bool:
        if node["kind"] == "group":
            outcomes = [evaluate(child) for child in node["children"]]
            return all(outcomes) if node["operator"] == "AND" else any(outcomes)
        matched = _evaluate_condition(node, metadata, case_sensitive)
        conditions.append(
            {
                "dsl": render_rule_dsl(node),
                "field": node["field"],
                "operator": node["operator"],
                "matched": matched,
            }
        )
        return matched

    return RuleEvaluation(matched=evaluate(ast), conditions=tuple(conditions))


def _fold(case_sensitive: bool, text: str) -> str:
    return text if case_sensitive else text.casefold()


def _tags(metadata: dict[str, str], case_sensitive: bool) -> set[str]:
    return {
        _fold(case_sensitive, item.strip())
        for field in ("TagsRaw", "Tags")
        for item in metadata.get(field, "").replace("\n", ",").split(",")
        if item.strip()
    }


def _evaluate_condition(
    node: dict[str, Any], metadata: dict[str, str], case_sensitive: bool
) -> bool:
    field = node["field"]
    operator = node["operator"]
    if field == COLLECTION_FIELD:
        return _evaluate_collection(node, metadata, case_sensitive)

    actual_raw = metadata.get(field, "").strip()
    if operator in EXISTENCE_OPS:
        return bool(actual_raw) if operator == "EXISTS" else not actual_raw
    if not actual_raw:
        return False

    if field in NUMERIC_FIELDS:
        return _evaluate_numeric(node, actual_raw)
    return _evaluate_text(node, actual_raw, case_sensitive)


def _evaluate_collection(
    node: dict[str, Any], metadata: dict[str, str], case_sensitive: bool
) -> bool:
    operator = node["operator"]
    tags = _tags(metadata, case_sensitive)
    value = node["value"]
    if operator in EXISTENCE_OPS:
        expected = _fold(case_sensitive, str(value))
        present = expected in tags
        return present if operator == "EXISTS" else not present
    if operator in IN_OPS:
        members = {_fold(case_sensitive, str(item)) for item in value}
        any_present = bool(members & tags)
        return any_present if operator == "IN" else not any_present
    if operator in LIKE_OPS:
        matched = any(
            _like_matches(node["value"], tag, case_sensitive) for tag in tags
        )
        return matched if operator == "LIKE" else not matched
    return False


def _evaluate_numeric(node: dict[str, Any], actual_raw: str) -> bool:
    operator = node["operator"]
    expected = node["value"]
    try:
        actual_number = float(actual_raw)
    except ValueError:
        return False
    if operator in IN_OPS:
        expected_numbers = {float(item) for item in expected}
        membership = actual_number in expected_numbers
        return membership if operator == "IN" else not membership
    if operator == "=":
        return actual_number == float(expected)
    if operator == "<>":
        return actual_number != float(expected)
    return {
        ">": actual_number > float(expected),
        ">=": actual_number >= float(expected),
        "<": actual_number < float(expected),
        "<=": actual_number <= float(expected),
    }[operator]


def _evaluate_text(
    node: dict[str, Any], actual_raw: str, case_sensitive: bool
) -> bool:
    operator = node["operator"]
    actual = _fold(case_sensitive, actual_raw)
    if operator in LIKE_OPS:
        matched = _like_matches(node["value"], actual_raw, case_sensitive)
        return matched if operator == "LIKE" else not matched
    if operator in IN_OPS:
        members = {_fold(case_sensitive, str(item)) for item in node["value"]}
        membership = actual in members
        return membership if operator == "IN" else not membership
    expected = _fold(case_sensitive, str(node["value"]))
    if operator == "=":
        return actual == expected
    if operator == "<>":
        return actual != expected
    return False


def _like_matches(pattern: str, actual: str, case_sensitive: bool) -> bool:
    """`actual` matches the LIKE `pattern`, honoring the rule's case setting.

    The whole pattern is case-folded before compile: its syntax characters
    (`%`, `_`, `[`, `]`) are punctuation unaffected by `casefold`, so folding
    the run of literals is safe and keeps non-ASCII folds (ß, …) matching the
    way Python's string fold does rather than the ASCII-only `re.IGNORECASE`.
    """
    folded_pattern = _fold(case_sensitive, pattern)
    folded_actual = _fold(case_sensitive, actual)
    return _like_pattern(folded_pattern).fullmatch(folded_actual) is not None


def _like_pattern(value: str) -> re.Pattern[str]:
    """Compile a LIKE pattern: `%` any run, `_` one char, `[%]` / `[_]` literal.

    These are the only `[...]` escapes recognised -- a `[` not part of `[%]` or
    `[_]` is treated as a literal character. The pattern is case-folded by the
    caller when the rule is case-insensitive, so the regex stays case-sensitive
    and a non-ASCII fold (ß, etc.) matches Python's fold rather than being
    limited to the ASCII-only `re.IGNORECASE`.
    """
    parts: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char in {"%", "_"}:
            parts.append(".*" if char == "%" else ".")
            index += 1
            continue
        if (
            char == "["
            and index + 2 < len(value)
            and value[index + 1] in {"%", "_"}
            and value[index + 2] == "]"
        ):
            parts.append(re.escape(value[index + 1]))
            index += 3
            continue
        parts.append(re.escape(char))
        index += 1
    return re.compile("".join(parts))


__all__ = [
    "ALL_OPERATORS",
    "ALLOWED_FIELDS",
    "COLLECTION_FIELD",
    "COLLECTION_OPERATORS",
    "NUMERIC_FIELDS",
    "NUMERIC_OPERATORS",
    "RuleEvaluation",
    "RuleValidationError",
    "TEXT_FIELDS",
    "TEXT_OPERATORS",
    "editor_rows",
    "evaluate_rule",
    "normalize_operator",
    "render_rule_dsl",
    "validate_rule_ast",
]