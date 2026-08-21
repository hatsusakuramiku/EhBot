"""Data shapes for the Telegraph preview-page image source."""

from __future__ import annotations

from dataclasses import dataclass


class TelegraphError(ValueError):
    """A failure the download queue can record verbatim.

    Mirrors ``ExHentaiDownloadError`` so the worker can lift ``code`` and
    ``public_message`` off any provider error without knowing the provider.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class TelegraphPage:
    path: str
    url: str
    title: str | None
    author: str | None
    image_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FetchedImage:
    name: str
    data: bytes
    source_url: str


@dataclass(frozen=True, slots=True)
class TelegraphFetchResult:
    """What a completed preview-page download produced.

    ``hosts`` is kept because the images never live on ``telegra.ph`` itself;
    knowing which third-party proxy served a book is what lets an operator
    tell a dead image host apart from a dead page.
    """

    page: TelegraphPage
    archive_path: str
    image_count: int
    total_bytes: int
    hosts: tuple[str, ...]


__all__ = [
    "FetchedImage",
    "TelegraphError",
    "TelegraphFetchResult",
    "TelegraphPage",
]