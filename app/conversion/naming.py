from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


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

#: The suffix appended after the name has been fitted, so every ceiling here is
#: measured on the name itself and never on the published string.
CBZ_SUFFIX = ".cbz"

#: What a filesystem that will not answer is assumed to take. The numbers are the
#: smallest in common use (ext4 / btrfs / xfs `NAME_MAX`, and a path budget no
#: shorter than any of them), so a name fitted to these works wherever the
#: library ends up. They are a floor for the *fallback*, not a project policy:
#: every real decision comes from `detect_library_limits`.
FALLBACK_NAME_MAX = 255
FALLBACK_PATH_MAX = 4096


@dataclass(frozen=True)
class LibraryLimits:
    """What the filesystem under the library root will take, in **bytes**.

    Detected rather than declared (R35): `NAME_MAX` is 255 *bytes* on ext4 /
    btrfs / xfs and 255 *characters* on NTFS, and any number this project picked
    would be wrong on one of them -- a character ceiling silently shortened
    names the disk would have taken, and one measured in bytes would refuse
    names a Windows share accepts. Bytes are what the POSIX call reports, and
    bytes are what the comparison uses.
    """

    #: Bytes for one path component -- a file name or a directory name, alike.
    name_max: int
    #: Bytes available for the library-*relative* part: `PATH_MAX` minus the
    #: absolute path of the library root and its separator.
    relative_max: int


#: What the pure functions use when nobody told them where the book is going.
#: Production paths pass a detected one; tests pass a small one on purpose.
FALLBACK_LIMITS = LibraryLimits(
    name_max=FALLBACK_NAME_MAX, relative_max=FALLBACK_PATH_MAX
)


def _pathconf(directory: Path, key: str, fallback: int) -> int:
    """One `os.pathconf` value, or the fallback when the platform has none.

    `os.pathconf` does not exist on Windows and raises on a filesystem that
    cannot answer, so this is a lookup with a floor rather than a check.
    """
    try:
        value = os.pathconf(directory, key)
    except (AttributeError, OSError, ValueError):
        return fallback
    return value if value > 0 else fallback


def detect_library_limits(root: Path | None) -> LibraryLimits:
    """Ask the filesystem that holds `root` what it accepts.

    The nearest *existing* ancestor answers -- `os.pathconf` needs a directory it
    can stat, and the library root may not have been created yet -- while the
    budget is computed from where the root is going to be, so a not-yet-created
    tree cannot buy itself a bigger allowance than it will have.

    `None` means no library directory is configured at all, which cannot pack
    anything either way; the probe falls back to the process's own directory so
    the answer is still *a* filesystem's rather than an exception.
    """
    base = Path(root) if root else Path.cwd()
    probe = base
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    name_max = _pathconf(probe, "PC_NAME_MAX", FALLBACK_NAME_MAX)
    path_max = _pathconf(probe, "PC_PATH_MAX", FALLBACK_PATH_MAX)
    root_bytes = len(str(base.resolve()).encode("utf-8"))
    return LibraryLimits(
        name_max=name_max,
        relative_max=max(path_max - root_bytes - 1, 0),
    )


def file_name_max(limits: LibraryLimits) -> int:
    """Bytes available to a file *stem*: what the filesystem measures is the
    published component, and that includes the suffix appended afterwards.

    Without this, a stem fitted to the whole allowance becomes an over-long
    component the moment `.cbz` lands on it -- a refusal our own check would not
    have predicted.
    """
    return max(limits.name_max - len(CBZ_SUFFIX.encode("utf-8")), 1)


def _fitted(value: str, name_max: int) -> str:
    """The longest prefix of `value` that fits in `name_max` bytes.

    Cut on a character boundary: half of a 「名」 is not a shorter name, it is
    mojibake in the library.
    """
    if len(value.encode("utf-8")) <= name_max:
        return value
    return value.encode("utf-8")[:name_max].decode("utf-8", "ignore")


def safe_library_name(
    value: str, *, fallback: str, name_max: int = FALLBACK_NAME_MAX
) -> str:
    """Normalize a metadata value into one safe library path segment.

    Repairs rather than refuses -- the book is already downloaded and has to land
    somewhere -- and `name_max` is the caller's, because only the caller knows
    which filesystem the name is about to land on.
    """
    normalized = unicodedata.normalize("NFC", value or "").strip()
    cleaned = _UNSAFE_CHARACTERS.sub(" ", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}-archive"
    cleaned = _fitted(cleaned, name_max).rstrip(" .")
    return cleaned or fallback


#: Placeholders the layout template understands. Each is metadata every book has
#: a value or a fallback for: a template is only useful if it renders for every
#: book that reaches the library, and a placeholder that is empty half the time
#: produces half a tree of 「未分类」.
#:
#: `japanese_title` and `english_title` name one language each, for a template
#: that wants to be explicit. `title` is the one that follows the 标题来源
#: setting, and it is what a template should normally use -- the setting exists
#: so an operator changes their mind once rather than editing the template.
TEMPLATE_PLACEHOLDERS: tuple[str, ...] = (
    "category",
    "artist",
    "title",
    "japanese_title",
    "english_title",
)

#: Words for the fields, so the settings page can explain the placeholders
#: without a second copy of the list in the template.
PLACEHOLDER_LABELS: dict[str, str] = {
    "category": "分类",
    "artist": "作者",
    "title": "标题（按设置）",
    "japanese_title": "日文标题",
    "english_title": "英文标题",
}

#: What a placeholder renders as when the metadata has no value for it. The three
#: title placeholders are absent on purpose: the caller always supplies a title
#: fallback derived from the candidate id, because a file named 「未命名」 for
#: every untitled book would collide with itself.
PLACEHOLDER_FALLBACKS: dict[str, str] = {
    "category": "未分类",
    "artist": "未知作者",
}

#: The placeholders that mean 「this book's name」. They share the caller's
#: `title_fallback` rather than a constant for the reason above, and they are
#: named as a set because both `render_library_path` and `plan_library_path`
#: have to treat all three alike -- a list written twice is how
#: `{english_title}` would fall back to an empty string in one of them.
TITLE_PLACEHOLDERS: frozenset[str] = frozenset(
    {"title", "japanese_title", "english_title"}
)

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
    * a template with no title placeholder at all gives every book in a group the
      same name, which the conflict suffix then papers over one `(2)` at a time.
      Any of the three counts: `{japanese_title}` names the book just as well as
      `{title}` does, and requiring the literal `{title}` would refuse a template
      that is more specific than the one being demanded.

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
    if not (
        set(_PLACEHOLDER_PATTERN.findall(template)) & TITLE_PLACEHOLDERS
    ):
        raise LibraryTemplateError(
            "TEMPLATE_NO_TITLE",
            "路径模板必须包含 {title}、{japanese_title} 或 {english_title}",
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
    limits: LibraryLimits = FALLBACK_LIMITS,
) -> Path:
    """Render one book's relative library path, sanitising every segment.

    Substitution happens per segment rather than on the whole string, and each
    result goes through `safe_library_name`, so a title containing a slash
    becomes part of one directory name instead of silently adding a level to the
    tree. That is the difference between an odd folder name and a metadata value
    choosing where a file lands.
    """
    segments = validate_library_template(template).split("/")
    rendered: list[str] = []
    for index, segment in enumerate(segments):
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            value = (values.get(name) or "").strip()
            if value:
                return value
            if name in TITLE_PLACEHOLDERS:
                return title_fallback
            return PLACEHOLDER_FALLBACKS.get(name, "")

        filled = _PLACEHOLDER_PATTERN.sub(substitute, segment)
        rendered.append(
            safe_library_name(
                filled,
                fallback=title_fallback,
                # The last segment is the file name, so it answers to the stem's
                # allowance (the suffix takes four bytes of the component); the
                # ones before it are directories, which get all of it.
                name_max=(
                    file_name_max(limits)
                    if index == len(segments) - 1
                    else limits.name_max
                ),
            )
        )
    return Path(*rendered)


class LibraryPathError(ValueError):
    """A rendered path this deployment will not publish a book to.

    Separate from `LibraryTemplateError` because the two are refused at
    different moments and mean different things to the operator. A template
    error is a setting they can fix; this is one *book* whose metadata renders
    into a name the filesystem cannot take, and the remedy is to edit that
    book's title or give it an explicit path. Sharing one exception would make
    「路径模板无效」the message for a book whose only problem is a 300-character
    title.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def check_library_segment(
    value: str, *, name_max: int = FALLBACK_NAME_MAX
) -> tuple[str, str] | None:
    """Why this path segment is unusable verbatim, or None if it is fine.

    The counterpart to `safe_library_name`, and deliberately not a variant of
    it: that function *repairs* a segment because a book already downloaded has
    to land somewhere, while this one *reports*. Both exist because the two
    moments differ. When the packer is resolving where a finished book goes,
    silently replacing `?` with a space is better than failing the job. When an
    operator is typing a path, or asking for a batch to be re-filed under the
    current template, a silent repair means the path they get is not the path
    they asked for and nothing on screen says so -- so there the answer is a
    refusal naming the reason.

    Returns `(code, message)` rather than raising so a batch can collect one
    reason per work without exception handling per segment.

    `name_max` is the caller's, for the reason `safe_library_name`'s is: only the
    caller knows which filesystem the segment is going to land on.
    """
    normalized = unicodedata.normalize("NFC", value or "")
    if not normalized.strip():
        return ("SEGMENT_EMPTY", "名称不能为空")
    if normalized != normalized.strip():
        return (
            "SEGMENT_PADDED",
            f"「{normalized.strip()}」前后有空格，请去掉",
        )
    if normalized in {".", ".."}:
        return ("SEGMENT_TRAVERSAL", "名称不能是 . 或 ..")
    found = _UNSAFE_CHARACTERS.findall(normalized)
    if found:
        # Control characters have no printable form, so they are named as a
        # class rather than echoed into the message.
        printable = sorted({char for char in found if char.isprintable()})
        shown = " ".join(printable) if printable else "控制字符"
        return (
            "SEGMENT_UNSAFE_CHARACTER",
            f"「{normalized}」含有不能用于路径的字符：{shown}",
        )
    encoded = len(normalized.encode("utf-8"))
    if encoded > name_max:
        return (
            "SEGMENT_TOO_LONG",
            f"「{normalized[:20]}…」占 {encoded} 字节，超过本机文件系统允许的"
            f"每个名字 {name_max} 字节，请缩短",
        )
    # A trailing dot or space is accepted by the API and then silently dropped
    # by Windows, which produces a file the database can no longer find.
    if normalized.endswith((".", " ")):
        return (
            "SEGMENT_TRAILING_DOT",
            f"「{normalized}」不能以点或空格结尾",
        )
    if normalized.lower() in _WINDOWS_RESERVED:
        return (
            "SEGMENT_RESERVED",
            f"「{normalized}」是系统保留名，请换一个",
        )
    return None


def strict_library_segment(
    value: str, *, name_max: int = FALLBACK_NAME_MAX
) -> str:
    """One path segment, unchanged, or a refusal explaining why not.

    `name_max` is the caller's, for the reason `safe_library_name`'s is.
    """
    refusal = check_library_segment(value, name_max=name_max)
    if refusal is not None:
        raise LibraryPathError(*refusal)
    return unicodedata.normalize("NFC", value)


def plan_library_path(
    template: str,
    values: dict[str, str | None],
    *,
    title_fallback: str,
    limits: LibraryLimits = FALLBACK_LIMITS,
) -> PurePosixPath:
    """The relative CBZ path the current template gives this book, or a refusal.

    The strict sibling of `render_library_path`, for the two callers that are
    acting on the operator's behalf right now rather than finishing a job: the
    archive-path form on the work detail page, and the batch re-file that
    recomputes every selected work's path from the current template. Both must
    be able to say「这本书没有动，因为……」, which a function that sanitises can
    never say.

    The `.cbz` suffix is appended, not `with_suffix`'d: 「Vol. 1」 would
    otherwise be read as a stem of `Vol` with a `. 1` extension and the book
    would be published as `Vol.cbz`.
    """
    segments = validate_library_template(template).split("/")
    rendered: list[str] = []
    for index, segment in enumerate(segments):

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            value = (values.get(name) or "").strip()
            if value:
                return value
            if name in TITLE_PLACEHOLDERS:
                return title_fallback
            return PLACEHOLDER_FALLBACKS.get(name, "")

        rendered.append(
            strict_library_segment(
                _PLACEHOLDER_PATTERN.sub(substitute, segment),
                # The last segment becomes the file name, so it answers to the
                # stem's allowance; the ones before it are directories.
                name_max=(
                    file_name_max(limits)
                    if index == len(segments) - 1
                    else limits.name_max
                ),
            )
        )
    relative = PurePosixPath(*rendered[:-1], f"{rendered[-1]}{CBZ_SUFFIX}")
    encoded = len(str(relative).encode("utf-8"))
    if encoded > limits.relative_max:
        raise LibraryPathError(
            "PATH_TOO_LONG",
            f"归档路径占 {encoded} 字节，超过本机文件系统给这个书库留下的 "
            f"{limits.relative_max} 字节（书库根目录之外的全部余量），"
            f"请缩短标题或改用更短的路径模板",
        )
    return relative


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
    "CBZ_SUFFIX",
    "DEFAULT_LIBRARY_TEMPLATE",
    "FALLBACK_LIMITS",
    "FALLBACK_NAME_MAX",
    "PLACEHOLDER_FALLBACKS",
    "PLACEHOLDER_LABELS",
    "TEMPLATE_PLACEHOLDERS",
    "TITLE_PLACEHOLDERS",
    "LibraryLimits",
    "LibraryPathError",
    "LibraryTemplateError",
    "check_library_segment",
    "detect_library_limits",
    "file_name_max",
    "plan_library_path",
    "render_library_path",
    "safe_library_name",
    "strict_library_segment",
    "unique_library_target",
    "validate_library_template",
]
