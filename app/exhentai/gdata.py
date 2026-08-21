from __future__ import annotations

import re
from dataclasses import dataclass, field


GALLERY_URL_RE = re.compile(
    r"https?://(?:exhentai\.org|e-hentai\.org)/g/(\d+)/([A-Za-z0-9]+)",
    flags=re.IGNORECASE,
)

# gdata returns tags as "namespace:value"; these map onto metadata fields.
TAG_NAMESPACES: tuple[str, ...] = (
    "language",
    "parody",
    "character",
    "group",
    "artist",
    "male",
    "female",
    "mixed",
    "other",
    "reclass",
    "temp",
)

# Namespaces that are descriptive rather than identifying, so they stay in Tags.
DESCRIPTIVE_NAMESPACES: frozenset[str] = frozenset(
    {"male", "female", "mixed", "other", "reclass", "temp"}
)


@dataclass(frozen=True, slots=True)
class GalleryTags:
    """Namespaced tags split by their gdata namespace."""

    by_namespace: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def get(self, namespace: str) -> tuple[str, ...]:
        return self.by_namespace.get(namespace, ())

    def first(self, namespace: str) -> str | None:
        values = self.get(namespace)
        return values[0] if values else None

    def flat(self) -> tuple[str, ...]:
        """Return every tag as "namespace:value", in namespace order."""
        ordered: list[str] = []
        for namespace in TAG_NAMESPACES:
            for value in self.get(namespace):
                ordered.append(f"{namespace}:{value}")
        for namespace, values in sorted(self.by_namespace.items()):
            if namespace in TAG_NAMESPACES:
                continue
            for value in values:
                ordered.append(f"{namespace}:{value}")
        return tuple(ordered)


def parse_tag_list(raw_tags) -> GalleryTags:
    """Group a gdata tag list into namespaces, preserving order.

    Tags without an explicit namespace fall into "misc", matching how
    E-Hentai displays unnamespaced tags.
    """
    grouped: dict[str, list[str]] = {}
    for raw in raw_tags or ():
        text = str(raw).strip()
        if not text:
            continue
        namespace, separator, value = text.partition(":")
        if separator:
            namespace = namespace.strip().lower()
            value = value.strip()
        else:
            namespace, value = "misc", text
        if not value:
            continue
        bucket = grouped.setdefault(namespace, [])
        if value not in bucket:
            bucket.append(value)
    return GalleryTags(
        by_namespace={key: tuple(values) for key, values in grouped.items()}
    )


def extract_gallery_ref(text: str) -> tuple[int, str] | None:
    """Return the (gid, token) of the first gallery URL in ``text``."""
    match = GALLERY_URL_RE.search(text or "")
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def _clean_title(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _to_int(value) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _to_float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class GalleryTorrent:
    """One torrent offered for a gallery, as listed by the gdata API.

    ``hash`` is the infohash only. The ``.torrent`` file itself lives behind
    ``gallerytorrents.php`` and needs a logged-in session.
    """

    hash: str
    name: str
    added: int | None
    tsize: int | None
    fsize: int | None

    @property
    def is_resample(self) -> bool:
        return "resample" in self.name.lower()


@dataclass(frozen=True, slots=True)
class GalleryData:
    """Structured metadata for one gallery, as returned by the gdata API."""

    gid: int
    token: str
    title: str | None
    title_jpn: str | None
    category: str | None
    uploader: str | None
    rating: float | None
    file_count: int | None
    file_size: int | None
    posted: str | None
    expunged: bool
    thumb: str | None
    tags: GalleryTags
    torrent_count: int = 0
    torrents: tuple[GalleryTorrent, ...] = ()

    @property
    def artists(self) -> tuple[str, ...]:
        return self.tags.get("artist")

    @property
    def groups(self) -> tuple[str, ...]:
        return self.tags.get("group")

    @property
    def parodies(self) -> tuple[str, ...]:
        return self.tags.get("parody")

    @property
    def characters(self) -> tuple[str, ...]:
        return self.tags.get("character")

    @property
    def languages(self) -> tuple[str, ...]:
        return self.tags.get("language")

    @property
    def primary_language(self) -> str | None:
        """Return the content language, ignoring translation markers."""
        for value in self.languages:
            if value.lower() not in {"translated", "rewrite", "speechless"}:
                return value
        return self.languages[0] if self.languages else None

    def gallery_url(self) -> str:
        return f"https://exhentai.org/g/{self.gid}/{self.token}/"

    @property
    def best_torrent(self) -> GalleryTorrent | None:
        return select_torrent(self.torrents, self.file_size)


def _parse_torrent_list(value: object) -> tuple[GalleryTorrent, ...]:
    """Read the ``torrents`` array, dropping entries without an infohash."""
    if not isinstance(value, list):
        return ()
    torrents: list[GalleryTorrent] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        digest = str(entry.get("hash") or "").strip().lower()
        if len(digest) != 40 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            continue
        torrents.append(
            GalleryTorrent(
                hash=digest,
                name=str(entry.get("name") or "").strip(),
                added=_to_int(entry.get("added")),
                tsize=_to_int(entry.get("tsize")),
                fsize=_to_int(entry.get("fsize")),
            )
        )
    return tuple(torrents)


def select_torrent(
    torrents: tuple[GalleryTorrent, ...], file_size: int | None
) -> GalleryTorrent | None:
    """Pick the torrent most likely to hold the original files.

    Resampled uploads are downscaled re-encodes, so they lose to any full
    torrent. Among the rest the one whose ``fsize`` sits closest to the
    gallery's own ``filesize`` wins; the two never match exactly, because a
    torrent carries the uploader's archive rather than the gallery pages.
    Ties fall back to the most recently added entry.
    """
    if not torrents:
        return None
    preferred = [item for item in torrents if not item.is_resample] or list(
        torrents
    )

    def rank(item: GalleryTorrent) -> tuple[int, int, int]:
        if file_size is None or item.fsize is None:
            distance = 1
        else:
            distance = abs(item.fsize - file_size)
        return (
            0 if item.fsize is not None else 1,
            distance,
            -(item.added or 0),
        )

    return min(preferred, key=rank)


def parse_gdata_entry(entry: dict) -> GalleryData | None:
    """Convert one ``gmetadata`` entry into a GalleryData.

    Returns None when the entry reports an error or lacks an identity.
    """
    if not isinstance(entry, dict) or entry.get("error"):
        return None
    gid = _to_int(entry.get("gid"))
    token = str(entry.get("token") or "").strip()
    if gid is None or not token:
        return None
    return GalleryData(
        gid=gid,
        token=token,
        title=_clean_title(entry.get("title")),
        title_jpn=_clean_title(entry.get("title_jpn")),
        category=_clean_title(entry.get("category")),
        uploader=_clean_title(entry.get("uploader")),
        rating=_to_float(entry.get("rating")),
        file_count=_to_int(entry.get("filecount")),
        file_size=_to_int(entry.get("filesize")),
        posted=_clean_title(entry.get("posted")),
        expunged=bool(entry.get("expunged")),
        thumb=_clean_title(entry.get("thumb")),
        tags=parse_tag_list(entry.get("tags")),
        torrent_count=_to_int(entry.get("torrentcount")) or 0,
        torrents=_parse_torrent_list(entry.get("torrents")),
    )


def gallery_data_to_metadata(gallery: GalleryData) -> dict[str, str]:
    """Flatten a GalleryData into the metadata field names EhBot stores."""
    fields: dict[str, str | None] = {
        "Title": gallery.title,
        "JapaneseTitle": gallery.title_jpn,
        "Category": gallery.category,
        "Uploader": gallery.uploader,
        "Artist": ", ".join(gallery.artists) or None,
        "Group": ", ".join(gallery.groups) or None,
        "Parody": ", ".join(gallery.parodies) or None,
        "Character": ", ".join(gallery.characters) or None,
        "Language": gallery.primary_language,
        "Tags": ", ".join(gallery.tags.flat()) or None,
        "Rating": (
            f"{gallery.rating:.2f}" if gallery.rating is not None else None
        ),
        "Pages": (
            str(gallery.file_count) if gallery.file_count else None
        ),
        "FileSize": (
            str(gallery.file_size) if gallery.file_size else None
        ),
        "Web": gallery.gallery_url(),
    }
    return {key: value for key, value in fields.items() if value}


__all__ = [
    "DESCRIPTIVE_NAMESPACES",
    "GALLERY_URL_RE",
    "GalleryData",
    "GalleryTags",
    "GalleryTorrent",
    "TAG_NAMESPACES",
    "extract_gallery_ref",
    "gallery_data_to_metadata",
    "parse_gdata_entry",
    "select_torrent",
    "parse_tag_list",
]
