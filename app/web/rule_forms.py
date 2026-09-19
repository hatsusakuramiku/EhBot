"""The condition-row form -> AST parser, shared by every rule editor.

Both the auto-approval tab and the paths tab submit their condition editor the
same way: parallel `condition_field` / `condition_operator` / `condition_value`
lists (what repeated HTML form names give natively), a `group_operator`, and a
row whose field is blank is skipped. One parser for both keeps that contract
single-source -- the macro that renders the rows, `settings.js` that previews
them, and this function that reads them all agree on the same names, so a field
renamed here stops meaning the same thing everywhere at once.
"""

from app.auto_approval.rules import (
    COLLECTION_FIELD,
    EXISTENCE_OPS,
    IN_OPS,
    normalize_operator,
)


def parse_rule_condition(form) -> dict | None:
    """Build one rule AST from the editor's submitted rows.

    A row whose field is blank is skipped, which is how the spare empty row the
    page always renders costs nothing.

    One row becomes that row's node rather than a group of one, matching what
    `render_rule_dsl` prints and what the browser previews: a simple rule
    should read simply in the stored DSL.

    The operator is normalised here (`not like` / `Not Like` / `NOT LIKE` ->
    `NOT_LIKE`) so the parser can decide the value's shape before `validate`
    runs: a list operator splits the comma-separated input into a list, an
    EXISTS on the TAG collection keeps its single tag, and a scalar EXISTS drops
    the value entirely. Whether a comparison needs a value at all is the
    validator's call, not this one's.
    """
    fields = form.getlist("condition_field")
    operators = form.getlist("condition_operator")
    values = form.getlist("condition_value")

    def at(items: list, index: int, default: str = "") -> str:
        """One row's value from a parallel list, or the default.

        The lists can be short of each other: a browser omits an unchecked
        control, and a hand-built request may send fewer of one name than
        another. Reading by index with a default keeps that a missing value
        rather than an IndexError.
        """
        return str(items[index]) if index < len(items) else default

    children: list[dict] = []
    for index, raw_field in enumerate(fields):
        field = str(raw_field or "").strip()
        if not field:
            continue
        operator = normalize_operator(at(operators, index))
        node: dict = {
            "kind": "condition",
            "field": field,
            "operator": operator,
        }
        if operator in IN_OPS:
            # A list operator gets a list, split the way `settings.js`
            # previews it, so 「chinese, futa」 means two values in both places.
            node["value"] = [
                item.strip()
                for item in at(values, index).split(",")
                if item.strip()
            ]
        elif operator in EXISTENCE_OPS:
            if field == COLLECTION_FIELD:
                node["value"] = at(values, index).strip()
        else:
            node["value"] = at(values, index).strip()
        children.append(node)
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    return {
        "kind": "group",
        "operator": normalize_operator(str(form.get("group_operator") or "AND")),
        "children": children,
    }