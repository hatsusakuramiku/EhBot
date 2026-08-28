"""Structural checks on rendered HTML, shared by the page tests.

These exist because two of the rules the pages depend on are enforced by the
browser's parser rather than by anything Python can see. A template that breaks
one of them renders without error, returns 200, and quietly does the wrong
thing when a person clicks — exactly the class of bug that reached R9 unnoticed
in the queue rows, where every per-row form was nested inside the batch form and
therefore dropped.
"""

from __future__ import annotations

from html.parser import HTMLParser

#: Elements with no end tag, so nothing restores the depth they consumed.
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


class _FormNesting(HTMLParser):
    """Collects `<form>` start tags the browser would throw away.

    A `<form>` start tag encountered inside another form is *ignored*: the
    element is never created, and its children — including its submit button and
    its `action` — join the outer form instead. Nothing warns about it.

    Content inside a `<template>` is exempt, and that exemption is the whole
    reason the confirmation dialogs work: a template's contents are parsed into a
    separate fragment, so a form in there is nested in nothing, and Alpine's
    `x-teleport` later moves it to `<body>` as an independent form.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._forms = 0
        self._templates = 0
        self.offending_lines: list[int] = []

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        """A self-closing tag opens and closes at once; ignore both halves."""

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "template":
            self._templates += 1
        elif tag == "form" and not self._templates:
            if self._forms:
                self.offending_lines.append(self.getpos()[0])
            self._forms += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "template" and self._templates:
            self._templates -= 1
        elif tag == "form" and not self._templates and self._forms:
            self._forms -= 1


def nested_form_lines(body: str) -> list[int]:
    """Line numbers of nested `<form>` tags the parser would discard."""
    parser = _FormNesting()
    parser.feed(body)
    return parser.offending_lines


class _GatedControls(HTMLParser):
    """Sorts submitting controls by whether a dialog stands in front of them.

    `role="dialog"` is the marker rather than a class name, because the point of
    the check is the thing a screen reader and a keyboard user act on: a control
    reached only after a second, deliberate step.

    A control is identified by where it sends the request — a button's
    `formaction`, or the `action` of the form a dialog carries — so a test can
    name an endpoint and ask whether reaching it takes one click or two.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._dialogs: list[int] = []
        self.gated: set[str] = set()
        self.ungated: set[str] = set()

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag: str, attrs: object) -> None:
        pairs = dict(attrs)  # type: ignore[arg-type]
        if tag not in _VOID:
            self._depth += 1
            if pairs.get("role") == "dialog":
                self._dialogs.append(self._depth)

        target = None
        if tag == "button" and pairs.get("type") == "submit":
            target = pairs.get("formaction") or pairs.get("form")
        elif tag == "form":
            target = pairs.get("action")
        if target:
            bucket = self.gated if self._dialogs else self.ungated
            bucket.add(target)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        while self._dialogs and self._dialogs[-1] >= self._depth:
            self._dialogs.pop()
        self._depth = max(0, self._depth - 1)


def _controls(body: str) -> _GatedControls:
    parser = _GatedControls()
    parser.feed(body)
    return parser


def gated_targets(body: str) -> set[str]:
    """Endpoints that can only be reached through a confirmation dialog."""
    return _controls(body).gated


def ungated_targets(body: str) -> set[str]:
    """Endpoints a single click submits."""
    return _controls(body).ungated
