from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.review.models import METADATA_FIELDS, RAW_METADATA_FIELDS


TEXT_OPERATORS = frozenset({"=", "!=", "CONTAINS", "STARTS_WITH", "LIKE"})
NUMERIC_OPERATORS = frozenset({"=", "!=", ">", ">=", "<", "<="})
COLLECTION_OPERATORS = frozenset({"HAS", "HAS_ANY", "HAS_ALL"})
EXISTENCE_OPERATORS = frozenset({"EXISTS", "NOT_EXISTS"})
ALL_OPERATORS = (
    TEXT_OPERATORS | NUMERIC_OPERATORS | COLLECTION_OPERATORS | EXISTENCE_OPERATORS
)
ALLOWED_FIELDS = frozenset(
    (*METADATA_FIELDS, *RAW_METADATA_FIELDS, "FileSize", "Web", "TAG")
)


class RuleValidationError(ValueError):
    """Raised when an automatic-approval rule AST is outside the DSL."""


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    matched: bool
    conditions: tuple[dict[str, Any], ...]


def validate_rule_ast(value: object) -> dict[str, Any]:
    """Validate and normalize a persisted automatic-approval AST."""
    if not isinstance(value, dict):
        raise RuleValidationError("规则必须是对象")
    kind = str(value.get("kind") or "").lower()
    if kind == "group":
        operator = str(value.get("operator") or "").upper()
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
    operator = str(value.get("operator") or "").upper()
    if field not in ALLOWED_FIELDS:
        raise RuleValidationError(f"不支持字段 {field}")
    if operator not in ALL_OPERATORS:
        raise RuleValidationError(f"不支持运算符 {operator}")
    if operator in COLLECTION_OPERATORS:
        if field != "TAG":
            raise RuleValidationError("集合运算符仅支持 TAG")
        raw_value = value.get("value")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        if operator in {"HAS_ANY", "HAS_ALL"} and not isinstance(raw_value, list):
            raise RuleValidationError(f"{operator} 的值必须是列表")
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if not cleaned:
            raise RuleValidationError("Tag 条件值不能为空")
        return {
            "kind": "condition",
            "field": field,
            "operator": operator,
            "value": cleaned if operator != "HAS" else cleaned[0],
        }
    if operator in EXISTENCE_OPERATORS:
        return {"kind": "condition", "field": field, "operator": operator}
    if "value" not in value or value["value"] is None:
        raise RuleValidationError("比较条件需要值")
    raw_value = value["value"]
    if operator in {">", ">=", "<", "<="}:
        try:
            normalized_value: str | float = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise RuleValidationError("数值比较条件需要数字") from exc
    else:
        normalized_value = str(raw_value).strip()
        if not normalized_value:
            raise RuleValidationError("比较条件值不能为空")
    return {
        "kind": "condition",
        "field": field,
        "operator": operator,
        "value": normalized_value,
    }


def render_rule_dsl(ast: dict[str, Any]) -> str:
    """Render a validated AST as readable, non-executable DSL text."""
    if ast["kind"] == "group":
        children = [render_rule_dsl(child) for child in ast["children"]]
        return "(" + f" {ast['operator']} ".join(children) + ")"
    field = "{TAG}" if ast["field"] == "TAG" else "{" + ast["field"] + "}"
    operator = ast["operator"]
    if operator in EXISTENCE_OPERATORS:
        return f"{field} {operator}"
    value = ast["value"]
    if isinstance(value, list):
        rendered = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, str):
        rendered = json.dumps(value, ensure_ascii=False)
    else:
        rendered = str(value)
    return f"{field} {operator} {rendered}"


def evaluate_rule(ast: dict[str, Any], metadata: dict[str, str]) -> RuleEvaluation:
    """Evaluate a validated AST against effective candidate metadata."""
    conditions: list[dict[str, Any]] = []

    def evaluate(node: dict[str, Any]) -> bool:
        if node["kind"] == "group":
            outcomes = [evaluate(child) for child in node["children"]]
            return all(outcomes) if node["operator"] == "AND" else any(outcomes)
        matched = _evaluate_condition(node, metadata)
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


def _evaluate_condition(node: dict[str, Any], metadata: dict[str, str]) -> bool:
    field = node["field"]
    operator = node["operator"]
    if field == "TAG":
        tags = _tags(metadata)
        if operator == "EXISTS":
            return bool(tags)
        if operator == "NOT_EXISTS":
            return not tags
        values = node.get("value")
        expected = (
            [str(value).casefold() for value in values]
            if isinstance(values, list)
            else [str(values).casefold()]
        )
        if operator == "HAS":
            return expected[0] in tags
        if operator == "HAS_ANY":
            return any(value in tags for value in expected)
        if operator == "HAS_ALL":
            return all(value in tags for value in expected)
        return False

    actual = metadata.get(field, "").strip()
    if operator == "EXISTS":
        return bool(actual)
    if operator == "NOT_EXISTS":
        return not actual
    if not actual:
        return False
    expected = node["value"]
    if operator in {">", ">=", "<", "<="}:
        try:
            actual_number = float(actual)
        except ValueError:
            return False
        return {
            ">": actual_number > expected,
            ">=": actual_number >= expected,
            "<": actual_number < expected,
            "<=": actual_number <= expected,
        }[operator]
    actual_folded = actual.casefold()
    expected_folded = str(expected).casefold()
    if operator == "=":
        return actual_folded == expected_folded
    if operator == "!=":
        return actual_folded != expected_folded
    if operator == "CONTAINS":
        return expected_folded in actual_folded
    if operator == "STARTS_WITH":
        return actual_folded.startswith(expected_folded)
    if operator == "LIKE":
        return bool(_like_pattern(expected_folded).fullmatch(actual_folded))
    return False


def _tags(metadata: dict[str, str]) -> set[str]:
    return {
        item.strip().casefold()
        for field in ("TagsRaw", "Tags")
        for item in metadata.get(field, "").replace("\n", ",").split(",")
        if item.strip()
    }


def _like_pattern(value: str) -> re.Pattern[str]:
    return re.compile(
        "".join(
            ".*" if character == "%" else "." if character == "_" else re.escape(character)
            for character in value
        )
    )


__all__ = [
    "ALLOWED_FIELDS",
    "ALL_OPERATORS",
    "RuleEvaluation",
    "RuleValidationError",
    "evaluate_rule",
    "render_rule_dsl",
    "validate_rule_ast",
]
