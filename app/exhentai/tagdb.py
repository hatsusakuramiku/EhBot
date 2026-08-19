from __future__ import annotations

import logging
from dataclasses import dataclass


LOGGER = logging.getLogger(__name__)

# "rows" holds the display names of the namespaces themselves, and
# "reclass" holds gallery category names, so neither is a tag namespace.
NON_TAG_NAMESPACES: frozenset[str] = frozenset({"rows", "reclass"})

# Namespaces searched when a tag arrives without an explicit namespace.
IMPLICIT_NAMESPACE_ORDER: tuple[str, ...] = (
    "female",
    "male",
    "mixed",
    "other",
    "language",
    "parody",
    "character",
    "artist",
    "group",
    "cosplayer",
    "location",
    "temp",
)


@dataclass(frozen=True, slots=True)
class TagTranslation:
    """One resolved translation."""

    namespace: str
    raw: str
    name: str
    intro: str = ""
    links: str = ""

    @property
    def qualified_raw(self) -> str:
        return f"{self.namespace}:{self.raw}"


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _reverse_rank(namespace: str) -> int:
    """Rank namespaces so reverse lookups prefer the conventional one.

    Several namespaces share a Chinese name (both female and male define
    巨乳), so the reverse index keeps the earliest namespace in
    IMPLICIT_NAMESPACE_ORDER rather than whichever happened to load last.
    """
    try:
        return IMPLICIT_NAMESPACE_ORDER.index(namespace)
    except ValueError:
        return len(IMPLICIT_NAMESPACE_ORDER)


class TagTranslator:
    """In-memory index over the EhTagTranslation database.

    Keys are ``namespace:raw`` so lookups are a single dict hit. Namespace
    display names and gallery categories are indexed separately because the
    upstream database stores them in the pseudo-namespaces "rows" and
    "reclass".
    """

    def __init__(self) -> None:
        self._tags: dict[str, TagTranslation] = {}
        self._by_namespace: dict[str, dict[str, TagTranslation]] = {}
        self._namespace_names: dict[str, str] = {}
        self._categories: dict[str, str] = {}
        self._reverse: dict[str, str] = {}
        self.version: str | None = None

    @property
    def entry_count(self) -> int:
        return len(self._tags)

    @property
    def is_loaded(self) -> bool:
        return bool(self._tags)

    def load(self, payload: dict) -> None:
        """Build the index from a parsed db.text.json payload."""
        tags: dict[str, TagTranslation] = {}
        by_namespace: dict[str, dict[str, TagTranslation]] = {}
        namespace_names: dict[str, str] = {}
        categories: dict[str, str] = {}
        reverse: dict[str, str] = {}

        for group in payload.get("data") or ():
            if not isinstance(group, dict):
                continue
            namespace = _normalize(group.get("namespace"))
            entries = group.get("data")
            if not namespace or not isinstance(entries, dict):
                continue
            for raw_key, value in entries.items():
                if not isinstance(value, dict):
                    continue
                name = str(value.get("name") or "").strip()
                if not name:
                    continue
                raw = _normalize(raw_key)
                if namespace == "rows":
                    namespace_names[raw] = name
                    continue
                if namespace == "reclass":
                    categories[raw] = name
                    continue
                translation = TagTranslation(
                    namespace=namespace,
                    raw=raw,
                    name=name,
                    intro=str(value.get("intro") or ""),
                    links=str(value.get("links") or ""),
                )
                tags[f"{namespace}:{raw}"] = translation
                by_namespace.setdefault(namespace, {})[raw] = translation
                reverse_key = _normalize(name)
                existing = reverse.get(reverse_key)
                if existing is None or _reverse_rank(
                    namespace
                ) < _reverse_rank(existing.partition(":")[0]):
                    reverse[reverse_key] = f"{namespace}:{raw}"

        self._tags = tags
        self._by_namespace = by_namespace
        self._namespace_names = namespace_names
        self._categories = categories
        self._reverse = reverse
        self.version = str(payload.get("version") or "") or None

    def lookup(
        self, namespace: str | None, raw: str
    ) -> TagTranslation | None:
        """Resolve one tag, probing common namespaces when none is given."""
        cleaned_raw = _normalize(raw)
        if not cleaned_raw:
            return None
        cleaned_namespace = _normalize(namespace) if namespace else ""
        if cleaned_namespace:
            if cleaned_namespace in NON_TAG_NAMESPACES:
                return None
            return self._tags.get(f"{cleaned_namespace}:{cleaned_raw}")
        for candidate in IMPLICIT_NAMESPACE_ORDER:
            found = self._by_namespace.get(candidate, {}).get(cleaned_raw)
            if found is not None:
                return found
        return None

    def translate_tag(self, tag: str) -> TagTranslation | None:
        """Resolve a "namespace:value" or bare tag string."""
        text = str(tag or "").strip()
        if not text:
            return None
        namespace, separator, value = text.partition(":")
        if separator:
            return self.lookup(namespace, value)
        return self.lookup(None, text)

    def translate_tags(
        self, tags: tuple[str, ...] | list[str]
    ) -> tuple[str, ...]:
        """Translate a tag list, keeping untranslated tags as-is."""
        translated: list[str] = []
        missing: list[str] = []
        for tag in tags:
            found = self.translate_tag(tag)
            if found is None:
                text = str(tag or "").strip()
                if text and text not in translated:
                    translated.append(text)
                    missing.append(text)
                continue
            if found.name not in translated:
                translated.append(found.name)
        if missing:
            LOGGER.info(
                "ehtag_missing_translations count=%d sample=%s",
                len(missing),
                ", ".join(missing[:5]),
            )
        return tuple(translated)

    def namespace_name(self, namespace: str) -> str | None:
        """Return the Chinese display name of a namespace."""
        return self._namespace_names.get(_normalize(namespace))

    def category_name(self, category: str) -> str | None:
        """Return the Chinese name of a gallery category."""
        return self._categories.get(_normalize(category))

    def raw_for_name(self, chinese_name: str) -> str | None:
        """Reverse lookup: Chinese name back to "namespace:raw"."""
        return self._reverse.get(_normalize(chinese_name))


__all__ = [
    "IMPLICIT_NAMESPACE_ORDER",
    "NON_TAG_NAMESPACES",
    "TagTranslation",
    "TagTranslator",
]
