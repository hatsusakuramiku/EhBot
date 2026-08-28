from __future__ import annotations

import re
import unicodedata
from pathlib import Path


# Reserved on Windows and unsafe in path segments everywhere else.
_UNSAFE_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)

MAX_SEGMENT_LENGTH = 120


def safe_library_name(value: str, *, fallback: str) -> str:
    """Normalize a metadata value into one safe library path segment."""
    normalized = unicodedata.normalize("NFC", value or "").strip()
    cleaned = _UNSAFE_CHARACTERS.sub(" ", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}-archive"
    if len(cleaned) > MAX_SEGMENT_LENGTH:
        cleaned = cleaned[:MAX_SEGMENT_LENGTH].rstrip(" .")
    return cleaned or fallback


#: Placeholders the layout template understands. Deliberately three, and
#: deliberately metadata every book has a value or a fallback for: a template is
#: only useful if it renders for every book that reaches the library, and a
#: placeholder that is empty half the time produces half a tree of 「未分类」.
TEMPLATE_PLACEHOLDERS: tuple[str, ...] = ("category", "artist", "title")

#: Words for the fields, so the settings page can explain the placeholders
#: without a second copy of the list in the template.
PLACEHOLDER_LABELS: dict[str, str] = {
    "category": "分类",
    "artist": "作者",
    "title": "标题",
}

#: What a placeholder renders as when the metadata has no value for it. `title`
#: is absent on purpose: the caller always supplies a title fallback derived
#: from the candidate id, because a file named 「未命名」 for every untitled book
#: would collide with itself.
PLACEHOLDER_FALLBACKS: dict[str, str] = {
    "category": "未分类",
    "artist": "未知作者",
}

_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]*)\}")

#: A flat library: every CBZ directly under the library root, which is what the
#: packer did before the layout was configurable. It stays the default so an
#: upgrade changes nothing about where books land until an operator says so.
DEFAULT_LIBRARY_TEMPLATE = "{title}"


class LibraryTemplateError(ValueError):
    """A layout template an operator may not save."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def validate_library_template(raw: str) -> str:
    """Accept a layout template, or explain exactly why it cannot be stored.

    Everything rejected here would otherwise fail hours later, inside a packing
    job, with the book already downloaded:

    * an unknown placeholder renders as literal `{publisher}` in a directory
      name, which reads as a bug in the output rather than in the template;
    * an absolute template escapes the library root, and `..` walks out of it,
      so both are path traversal expressed as a setting;
    * a template with no `{title}` gives every book in a group the same name,
      which the conflict suffix then papers over one `(2)` at a time.

    The returned value is normalized to forward slashes so one stored template
    means the same tree on both platforms.
    """
    template = (raw or "").strip().replace("\\", "/")
    if not template:
        raise LibraryTemplateError(
            "TEMPLATE_EMPTY", "路径模板不能为空"
        )
    if template.startswith("/"):
        raise LibraryTemplateError(
            "TEMPLATE_ABSOLUTE", "路径模板必须是相对于书库目录的路径"
        )
    unknown = [
        name
        for name in _PLACEHOLDER_PATTERN.findall(template)
        if name not in TEMPLATE_PLACEHOLDERS
    ]
    if unknown:
        raise LibraryTemplateError(
            "TEMPLATE_UNKNOWN_FIELD",
            f"路径模板里的 {{{unknown[0]}}} 不是可用字段",
        )
    if "{title}" not in template:
        raise LibraryTemplateError(
            "TEMPLATE_NO_TITLE", "路径模板必须包含 {title}"
        )
    segments = [segment for segment in template.split("/") if segment]
    if not segments:
        raise LibraryTemplateError(
            "TEMPLATE_EMPTY", "路径模板不能为空"
        )
    if any(segment in {".", ".."} for segment in segments):
        raise LibraryTemplateError(
            "TEMPLATE_TRAVERSAL", "路径模板不能包含 .. 或 ."
        )
    return "/".join(segments)


def render_library_path(
    template: str,
    values: dict[str, str | None],
    *,
    title_fallback: str,
) -> Path:
    """Render one book's relative library path, sanitising every segment.

    Substitution happens per segment rather than on the whole string, and each
    result goes through `safe_library_name`, so a title containing a slash
    becomes part of one directory name instead of silently adding a level to the
    tree. That is the difference between an odd folder name and a metadata value
    choosing where a file lands.
    """
    rendered: list[str] = []
    for segment in validate_library_template(template).split("/"):
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            value = (values.get(name) or "").strip()
            if value:
                return value
            if name == "title":
                return title_fallback
            return PLACEHOLDER_FALLBACKS.get(name, "")

        filled = _PLACEHOLDER_PATTERN.sub(substitute, segment)
        rendered.append(safe_library_name(filled, fallback=title_fallback))
    return Path(*rendered)


def unique_library_target(
    target: Path, *, reserved: frozenset[str] = frozenset()
) -> Path:
    """Return a free path for `target`, keeping a path this book already owns.

    Two different books can render the same name -- a group publishing the same
    title twice, or two titles that sanitise alike -- and overwriting one with
    the other loses a book with no trace. So an occupied name gains a ` (2)`
    suffix.

    `reserved` is the set of paths already recorded as this book's own archive,
    and a match in it wins over the file existing: re-packing must land on the
    file it is replacing, otherwise every 重新打包 would leave the previous CBZ
    behind and grow the suffix by one.
    """
    if str(target) in reserved:
        return target
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for attempt in range(2, 1000):
        candidate = target.with_name(f"{stem} ({attempt}){suffix}")
        if str(candidate) in reserved or not candidate.exists():
            return candidate
    # A thousand books with one name is not a naming problem any more, and a
    # silent overwrite would be the worse answer.
    raise LibraryTemplateError(
        "TEMPLATE_CONFLICT", f"同名文件过多，无法为 {target.name} 取到新名字"
    )


__all__ = [
    "DEFAULT_LIBRARY_TEMPLATE",
    "MAX_SEGMENT_LENGTH",
    "PLACEHOLDER_FALLBACKS",
    "PLACEHOLDER_LABELS",
    "TEMPLATE_PLACEHOLDERS",
    "LibraryTemplateError",
    "render_library_path",
    "safe_library_name",
    "unique_library_target",
    "validate_library_template",
]
