"""Reading a settings form, once, for the page and the JSON API.

The settings tabs are written as forms and R74/R75 added JSON callers for the
same writes. Three things have to behave identically in both worlds, and each is
small enough to get subtly different if it were written twice:

* **absence means off.** An unchecked HTML checkbox sends nothing at all, so a
  reader that defaulted an omitted key to true would make unchecking a switch
  silently keep it on.
* **one value or many.** `request.form()` returns a single value per key unless
  asked, while a JSON body carries a list; a reader that iterated a lone string
  would walk it character by character.
* **whitespace and case.** Tag lists are comma- or newline-separated and are
  compared against lowercase metadata, so the cleaning lives here rather than in
  each reader.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: The form fields that may carry more than one value. Named here rather than at
#: each call site so `form_values` cannot forget one and hand a reader a scalar
#: where it expects a list.
MULTI_VALUE_FIELDS: tuple[str, ...] = (
    "allowed_archive_formats",
    "archive_formats",
    "source_ids",
    "dialogs",
)


def items_of(raw: object) -> list[Any]:
    """One value or many, as a list.

    A lone string is a list of one rather than a sequence of characters: a form
    or a JSON body that sent `"ntr"` means one tag called `ntr`.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        return [raw]
    if isinstance(raw, Sequence):
        return list(raw)
    return [raw]


def checkbox(raw: object) -> bool:
    """A switch from either caller.

    The page sends `"on"` for a checked box and nothing for an unchecked one, so
    absence is false; JSON sends `true`/`false`. Anything else is read as false
    rather than refused -- an unchecked box is the safe reading, and it is what
    the page means by a missing field.
    """
    if isinstance(raw, bool):
        return raw
    return str(raw) == "on"


def form_values(form: Any) -> dict[str, Any]:
    """A Starlette form as a plain mapping, repeats kept as lists."""
    values: dict[str, Any] = dict(form)
    for key in MULTI_VALUE_FIELDS:
        values[key] = form.getlist(key)
    return values


def as_values(submission: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON body as a plain mapping (a copy, so a reader cannot mutate it)."""
    return dict(submission)


__all__ = [
    "MULTI_VALUE_FIELDS",
    "as_values",
    "checkbox",
    "form_values",
    "items_of",
]
