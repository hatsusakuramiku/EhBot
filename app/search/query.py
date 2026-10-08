"""The search box: `$…$` filter blocks plus free text (R61).

The operator's grammar, which is also the one asmr.one uses, with the automatic
rule DSL's wildcards folded in:

    query      := (free-text | block)*
    block      := "$" condition (" " condition)* "$"
    condition  := ["-"] field ":" value
    value      := (char | wildcard | escape)+
    wildcard   := "%" | "_"          # % any run, _ one character
    escape     := "[%]" | "[_]"      # the literal characters

Anything outside a block is free text: a global search over the same fields the
candidate list has always searched. A block restricts one condition to one
field. Blocks and free words are all ANDed. Values with spaces go in double
quotes: `$tag:"sole female"$`.

A fragment that cannot be parsed is **not an error**. It degrades to free text
and the caller shows a line saying so -- the operator's rule 2. A search box
that returned 500 for a stray `$` would be worse than one that searches for it,
so `parse_query` never raises. A `$` with no partner counts as one of those
fragments: the operator is told it was not read as a filter, and its words go
to the global search.

The field vocabulary is the rule editor's own (`ALLOWED_FIELDS`), so a field the
rules understand is a field you can search, minus the numeric ones: the operator
decided numbers are not searchable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.api.status import CANDIDATE_STATUS, provider_codes_matching
from app.auto_approval.rules import ALLOWED_FIELDS

#: Fields the operator decided are not searchable. They are in the rule
#: vocabulary but comparing a number in a search box is a filter, not a search,
#: and the operator asked for it out.
EXCLUDED_FIELDS: frozenset[str] = frozenset({"Rating", "Pages", "FileSize"})

#: Stored metadata fields that hold several comma-joined values, so a
#: whole-value match has to compare one item rather than the whole string.
MULTI_VALUE_FIELDS: frozenset[str] = frozenset(
    {
        "Tags",
        "TagsRaw",
        "Artist",
        "ArtistRaw",
        "Group",
        "GroupRaw",
        "Parody",
        "ParodyRaw",
        "Character",
        "CharacterRaw",
    }
)

#: The tag collection: `tag` is offered in the rule editor as a pseudo-field and
#: matches the translated tags and the upstream ones together.
TAG_FIELD = "TAG"
TAG_FIELDS: tuple[str, ...] = ("Tags", "TagsRaw")

#: Fields a free-text word is matched against. Deliberately broad -- a word is
#: the operator saying 「somewhere」 -- and it includes the raw upstream spellings
#: the old substring search already covered.
GLOBAL_FIELDS: tuple[str, ...] = (
    "Title",
    "JapaneseTitle",
    "Artist",
    "ArtistRaw",
    "Group",
    "GroupRaw",
    "Parody",
    "ParodyRaw",
    "Character",
    "CharacterRaw",
    "Tags",
    "TagsRaw",
    "Category",
    "CategoryRaw",
    "Language",
    "Uploader",
)

#: `title` -> `Title`, `tagsraw` -> `TagsRaw`, ... Built from the rule
#: vocabulary so the two cannot drift. `TAG` (the collection) is in there too.
_FIELD_NAMES: dict[str, str] = {
    name.casefold(): name
    for name in ALLOWED_FIELDS
    if name not in EXCLUDED_FIELDS
}

#: Spellings the operator asked for that are not the stored name.
_FIELD_ALIASES: dict[str, str] = {
    "jtitle": "JapaneseTitle",
    "tag": TAG_FIELD,
    "tags": TAG_FIELD,
}

#: Page-scoped pseudo-fields. `status` names a candidate state, `source` the
#: provider that produced a download; neither is metadata, so neither lives in
#: `metadata_values`.
STATUS_FIELD = "status"
SOURCE_FIELD = "source"

PAGE_CANDIDATES = "candidates"
PAGE_DOWNLOADED = "downloaded"

#: What a chip calls each resolved field. Falls back to the stored name.
_FIELD_LABELS: dict[str, str] = {
    TAG_FIELD: "标签",
    "Title": "标题",
    "JapaneseTitle": "日文标题",
    "Artist": "作者",
    "Group": "社团",
    "Parody": "原作",
    "Character": "角色",
    "Category": "分类",
    "Language": "语言",
    "Uploader": "上传者",
    "Tags": "标签",
    "TagsRaw": "原始标签",
    STATUS_FIELD: "状态",
    SOURCE_FIELD: "来源",
}

_LIKE_ESCAPE = "\\"
_ESCAPED_LITERALS = {"[%]": "%", "[_]": "_"}

#: `field:value`, with an optional leading `-`. The field is restricted to the
#: shape the vocabulary uses so a URL's `https:` cannot be read as one.
_CONDITION = re.compile(r"^(-?)([A-Za-z_][A-Za-z0-9_]*):(.*)$", re.DOTALL)


def like_pattern(value: str) -> str:
    """Translate a search value into the body of a SQL LIKE pattern.

    `%` and `_` stay wildcards -- the rule DSL's semantics, which the operator
    asked to reuse -- while `[%]` and `[_]` name the literal characters. A
    backslash, the ESCAPE character, is always literal, so it is doubled; a
    bare `[` is escaped so it cannot start another literal group. The result is
    for use with ``LIKE ... ESCAPE '\\'``.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        triple = value[index : index + 3]
        if triple in _ESCAPED_LITERALS:
            out.append(_LIKE_ESCAPE + _ESCAPED_LITERALS[triple])
            index += 3
            continue
        char = value[index]
        if char == "\\":
            out.append(_LIKE_ESCAPE + _LIKE_ESCAPE)
        elif char == "[":
            out.append(_LIKE_ESCAPE + "[")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def has_wildcard(value: str) -> bool:
    """Whether a value carries a wildcard once `[%]`/`[_]` are taken out.

    Decides the chip's verb: a value with no wildcard is a whole-value match
    (`=`), one with a wildcard is a pattern (`≈`).
    """
    index = 0
    while index < len(value):
        if value[index : index + 3] in _ESCAPED_LITERALS:
            index += 3
            continue
        if value[index] in "%_":
            return True
        index += 1
    return False


@dataclass(frozen=True, slots=True)
class _Word:
    """One whitespace-separated token, with where it sat in the whole query."""

    text: str
    start: int
    end: int


def _split_words(text: str) -> list[_Word] | None:
    """Split on whitespace, keeping `"…"` / `'…'` runs together.

    Offsets are carried so a chip can point at the exact slice of the search
    box it removes. Returns None when a quote is left open: the fragment cannot
    be read, and the caller degrades it rather than guessing where the value
    ended.
    """
    words: list[_Word] = []
    current: list[str] = []
    begin: int | None = None
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
            if begin is None:
                begin = index
        elif char.isspace():
            if begin is not None:
                words.append(_Word("".join(current), begin, index))
                current = []
                begin = None
        else:
            if begin is None:
                begin = index
            current.append(char)
    if quote is not None:
        return None
    if begin is not None:
        words.append(_Word("".join(current), begin, len(text)))
    return words


@dataclass(frozen=True, slots=True)
class _Segment:
    """One piece of the query: a block, free text, or a block that never closed.

    `open` and `close` are indices of the `$` delimiters in the whole query, so
    a chip can excise the block when its last condition is removed.
    """

    kind: str  # "block" | "text" | "unclosed"
    content: str
    open: int
    close: int | None


def _segments(text: str) -> list[_Segment]:
    """Split a query into pieces, in order.

    A `$` with no partner is its own kind rather than being folded into the
    free text: the operator gets told that the fragment was not read as a
    filter, and the words still go to the global search (proposal §2.4). That
    totality is what keeps a stray `$` from 500ing the page.
    """
    parts: list[_Segment] = []
    index = 0
    while index < len(text):
        if text[index] == "$":
            closing = text.find("$", index + 1)
            if closing == -1:
                parts.append(_Segment("unclosed", text[index:], index, None))
                break
            parts.append(
                _Segment("block", text[index + 1 : closing], index, closing)
            )
            index = closing + 1
            continue
        closing = text.find("$", index)
        if closing == -1:
            closing = len(text)
        parts.append(_Segment("text", text[index:closing], index, None))
        index = closing
    return parts


@dataclass(frozen=True, slots=True)
class Filter:
    """One parsed `$field:value$` condition."""

    field: str
    value: str
    exclude: bool
    fuzzy: bool
    #: The condition exactly as the operator wrote it, quotes and `-` included.
    #: A chip's × hands it back to `ParsedQuery.without`, so it has to match the
    #: search box character for character.
    raw: str
    #: Resolved codes for the page-scoped pseudo-fields (`status` / `source`).
    codes: tuple[str, ...] = ()
    #: (start, end) of the enclosing `$…$` in the query text. The × drops the
    #: whole block when this was its last condition -- leaving `$ $` behind
    #: would read as a broken empty block on the next submit.
    block: tuple[int, int] | None = None

    @property
    def label(self) -> str:
        return _FIELD_LABELS.get(self.field, self.field)

    @property
    def chip(self) -> str:
        joiner = " ≈ " if self.fuzzy else " = "
        return f"{'不含 ' if self.exclude else ''}{self.label}{joiner}{self.value}"

    def to_payload(self) -> dict[str, object]:
        return {
            "field": self.field,
            "label": self.label,
            "value": self.value,
            "exclude": self.exclude,
            "fuzzy": self.fuzzy,
            "raw": self.raw,
        }


@dataclass(frozen=True, slots=True)
class FreeTerm:
    """One word outside a block: a global search for it."""

    value: str
    exclude: bool = False


@dataclass(frozen=True, slots=True)
class Degraded:
    """A fragment that could not be read, kept so the page can say so."""

    text: str
    reason: str


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    text: str
    filters: tuple[Filter, ...] = ()
    terms: tuple[FreeTerm, ...] = ()
    degraded: tuple[Degraded, ...] = ()

    @property
    def active(self) -> bool:
        """Whether anything at all was searched for."""
        return bool(self.filters or self.terms)

    def to_payload(self) -> dict[str, object]:
        return {
            "filters": [item.to_payload() for item in self.filters],
            "terms": [
                {"value": item.value, "exclude": item.exclude}
                for item in self.terms
            ],
            "degraded": [
                {"text": item.text, "reason": item.reason}
                for item in self.degraded
            ],
        }

    def without(self, raw: str) -> str:
        """The query text with one condition removed, for a chip's × link.

        Excises the condition and collapses the whitespace that is left, so
        deleting a middle chip does not leave a double space behind. When it
        was the last condition inside its `$…$`, the delimiters go too.
        """
        target = next((item for item in self.filters if item.raw == raw), None)
        index = self.text.find(raw)
        if index == -1:
            return self.text
        end = index + len(raw)
        if target is not None and target.block is not None:
            others = [
                item
                for item in self.filters
                if item.block == target.block and item.raw != raw
            ]
            if not others:
                open_index, close_index = target.block
                return _collapse(
                    self.text[:open_index] + self.text[close_index + 1 :]
                )
        return _collapse(self.text[:index] + " " + self.text[end:])


def _collapse(text: str) -> str:
    """Squash the whitespace a removal left behind."""
    return re.sub(r"\s+", " ", text).strip()


def _resolve_field(name: str, page: str) -> tuple[str | None, str | None]:
    """`(canonical field, refusal)`. Exactly one is set."""
    lowered = name.casefold()
    if lowered == STATUS_FIELD:
        if page == PAGE_CANDIDATES:
            return STATUS_FIELD, None
        return None, "status 只在候选页可用"
    if lowered == SOURCE_FIELD:
        if page == PAGE_DOWNLOADED:
            return SOURCE_FIELD, None
        return None, "source 只在已下载页可用"
    canonical = _FIELD_ALIASES.get(lowered) or _FIELD_NAMES.get(lowered)
    if canonical is None:
        return None, f"不支持的字段 {name}"
    return canonical, None


def _status_codes(value: str) -> tuple[str, ...]:
    """Codes a `status:` value refers to, by code or by the label the page shows."""
    needle = value.strip().casefold()
    if not needle:
        return ()
    for code, view in CANDIDATE_STATUS.items():
        if code.casefold() == needle or view.label.casefold() == needle:
            return (code,)
    return ()


def _block_filters(
    segment: _Segment, page: str
) -> tuple[list[Filter], str | None]:
    """Parse one `$…$` block. Returns its filters, or a refusal reason.

    All or nothing: a block with one unreadable condition degrades as a whole
    and its words fall back to the global search, which is what the operator
    asked for in rule 2 and what the page's notice line explains. Splitting the
    difference -- honouring half a block silently -- would make the notice lie.
    """
    content = segment.content
    words = _split_words(content)
    if words is None:
        return [], "引号没有闭合"
    if not words:
        return [], "空的过滤块"
    filters: list[Filter] = []
    for word in words:
        match = _CONDITION.match(word.text)
        if match is None or not match.group(3):
            return [], f"无法识别的条件 {word.text}"
        exclude = bool(match.group(1))
        canonical, refusal = _resolve_field(match.group(2), page)
        if canonical is None:
            return [], refusal or "无法识别的字段"
        value = match.group(3)
        codes: tuple[str, ...] = ()
        if canonical == STATUS_FIELD:
            codes = _status_codes(value)
            if not codes:
                return [], f"未知状态 {value}"
        elif canonical == SOURCE_FIELD:
            codes = provider_codes_matching(value)
            if not codes:
                return [], f"未知来源 {value}"
        filters.append(
            Filter(
                field=canonical,
                value=value,
                exclude=exclude,
                fuzzy=has_wildcard(value),
                raw=content[word.start : word.end],
                codes=codes,
                block=(
                    None if segment.close is None else (segment.open, segment.close)
                ),
            )
        )
    return filters, None


def _add_free_text(text: str, terms: list[FreeTerm]) -> str | None:
    """Turn a piece of free text into global-search terms.

    Returns a refusal reason when the piece could not be split (an unclosed
    quote). Even then the words are still searched for -- dropping them would
    make 「已按全局搜索处理」 a lie -- by falling back to a plain whitespace
    split with the quotes taken out.
    """
    words = _split_words(text)
    refusal: str | None = None
    if words is None:
        refusal = "引号没有闭合"
        quoted = text.replace('"', " ").replace("'", " ")
        words = [_Word(part, 0, 0) for part in quoted.split()]
    for word in words:
        exclude = word.text.startswith("-") and len(word.text) > 1
        value = word.text[1:] if exclude else word.text
        if value:
            terms.append(FreeTerm(value=value, exclude=exclude))
    return refusal


def parse_query(text: str, *, page: str = PAGE_CANDIDATES) -> ParsedQuery:
    """Read a search box into filters, free terms and degraded fragments.

    Total by construction: every branch either produces a filter or records a
    degradation, so the caller never has to handle a parse failure.
    """
    raw = text or ""
    filters: list[Filter] = []
    terms: list[FreeTerm] = []
    degraded: list[Degraded] = []
    for segment in _segments(raw):
        if segment.kind == "block":
            parsed, refusal = _block_filters(segment, page)
            if refusal is not None:
                degraded.append(Degraded(f"${segment.content}$", refusal))
                _add_free_text(segment.content, terms)
                continue
            filters.extend(parsed)
            continue
        if segment.kind == "unclosed":
            degraded.append(Degraded(segment.content, "过滤块没有闭合"))
            _add_free_text(segment.content, terms)
            continue
        refusal = _add_free_text(segment.content, terms)
        if refusal is not None:
            degraded.append(Degraded(segment.content.strip(), refusal))
    return ParsedQuery(
        text=raw, filters=tuple(filters), terms=tuple(terms),
        degraded=tuple(degraded),
    )


@dataclass(frozen=True, slots=True)
class SearchSql:
    """A WHERE fragment and its parameters. `clause` is never empty."""

    clause: str
    params: tuple[object, ...]


#: Match one value inside a comma-joined field, wrapping it in commas so
#: 「loli」 does not match 「lolicon」. Mirrors the sidebar facet's own fence.
def _fence(expression: str) -> str:
    return (
        "',' || replace(replace(" + expression + ", char(10), ','), ', ', ',') "
        "|| ','"
    )


def _exists(fields: Sequence[str], predicate: str, params: list, alias: str):
    placeholders = ", ".join("?" for _ in fields)
    sql = (
        f"EXISTS (SELECT 1 FROM metadata_values mv "
        f"WHERE mv.candidate_id = {alias}.id "
        f"AND mv.field_name IN ({placeholders}) AND {predicate})"
    )
    return sql, [*fields, *params]


def _metadata_clause(item: Filter, alias: str) -> tuple[str, list]:
    """One metadata condition as an EXISTS/NOT EXISTS fragment."""
    fields = TAG_FIELDS if item.field == TAG_FIELD else (item.field,)
    body = like_pattern(item.value)
    escape = f" ESCAPE '{_LIKE_ESCAPE}'"
    if item.value == "":
        return "1=1", []
    if item.fuzzy:
        # The operator wrote the wildcards, so the pattern is theirs -- no
        # extra % wrapping. One LIKE covers a whole multi-value field as well
        # as a single one, because the item text is still a substring of it.
        predicate = f"mv.field_value LIKE ?{escape}"
        params = [body]
    elif item.field == TAG_FIELD:
        # A translated tag matches as an item; an upstream `namespace:tag`
        # matches with or without its namespace, so `$tag:netorare$` finds
        # `female:netorare` too.
        fenced = _fence("mv.field_value")
        predicate = f"({fenced} LIKE ?{escape} OR {fenced} LIKE ?{escape})"
        params = [f"%,{body},%", f"%:{body},%"]
    elif item.field in MULTI_VALUE_FIELDS:
        predicate = f"{_fence('mv.field_value')} LIKE ?{escape}"
        params = [f"%,{body},%"]
    else:
        predicate = f"mv.field_value LIKE ?{escape}"
        params = [body]
    sql, params = _exists(fields, predicate, params, alias)
    return (f"NOT {sql}" if item.exclude else sql), params


def _pseudo_clause(item: Filter, *, status_alias: str, provider_alias: str):
    if item.field == STATUS_FIELD:
        placeholders = ", ".join("?" for _ in item.codes)
        sql = f"{status_alias}.status IN ({placeholders})"
        return (f"NOT ({sql})" if item.exclude else sql), list(item.codes)
    placeholders = ", ".join("?" for _ in item.codes)
    sql = f"{provider_alias}.provider IN ({placeholders})"
    return (f"NOT ({sql})" if item.exclude else sql), list(item.codes)


def _free_clause(
    item: FreeTerm, alias: str, provider_alias: str | None
) -> tuple[str, list]:
    body = like_pattern(item.value)
    predicate = f"mv.field_value LIKE ? ESCAPE '{_LIKE_ESCAPE}'"
    sql, params = _exists(GLOBAL_FIELDS, predicate, [f"%{body}%"], alias)
    if provider_alias is not None:
        # A word also matches the provider that produced a download, by code or
        # by the Chinese label the page shows. Kept from the previous search box
        # so 「EH 归档」 keeps working without knowing about `source:`.
        codes = provider_codes_matching(item.value)
        if codes:
            placeholders = ", ".join("?" for _ in codes)
            sql = f"({sql} OR {provider_alias}.provider IN ({placeholders}))"
            params.extend(codes)
    return (f"NOT {sql}" if item.exclude else sql), params


def _compile(
    parsed: ParsedQuery, alias: str, provider_alias: str | None
) -> SearchSql:
    clauses: list[str] = []
    params: list[object] = []
    for term in parsed.terms:
        sql, term_params = _free_clause(term, alias, provider_alias)
        clauses.append(sql)
        params.extend(term_params)
    for item in parsed.filters:
        if item.field in (STATUS_FIELD, SOURCE_FIELD):
            sql, item_params = _pseudo_clause(
                item, status_alias=alias, provider_alias=provider_alias
            )
        else:
            sql, item_params = _metadata_clause(item, alias)
        clauses.append(sql)
        params.extend(item_params)
    if not clauses:
        return SearchSql("1=1", ())
    return SearchSql("(" + " AND ".join(clauses) + ")", tuple(params))


def candidate_search_sql(parsed: ParsedQuery) -> SearchSql:
    """The candidate list's WHERE fragment for a parsed query."""
    return _compile(parsed, "c", None)


def downloaded_search_sql(parsed: ParsedQuery) -> SearchSql:
    """The 已下载 list's WHERE fragment for a parsed query."""
    return _compile(parsed, "c", "dj")


__all__ = [
    "Degraded",
    "Filter",
    "FreeTerm",
    "PAGE_CANDIDATES",
    "PAGE_DOWNLOADED",
    "ParsedQuery",
    "SearchSql",
    "candidate_search_sql",
    "downloaded_search_sql",
    "has_wildcard",
    "like_pattern",
    "parse_query",
    "provider_codes_matching",
]
