"""One Telethon message in, one `ParsedSourceMessage` out.

The bot parser has a whole file of tests already; this one exists because the
MTProto path is a second translation of the same idea, and the fields a
mistake here would break are the ones the rest of the pipeline reads without
checking: the attachment coordinates a download needs, and the missing
`file_id` that tells routing the Bot API cannot fetch this file.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.candidates.mtproto import parse_user_message


class FakeEntity:
    """Telethon's `MessageEntityTextUrl`, reduced to what the parser reads."""

    def __init__(self, url: str) -> None:
        self.url = url


class FakeBareEntity:
    """An entity with no target -- `MessageEntityBold` and friends."""


class FakeDocument:
    def __init__(self, *, id: int = 7, size: int = 4096, mime: str = "application/zip") -> None:
        self.id = id
        self.size = size
        self.mime_type = mime


class FakeFile:
    def __init__(self, name: str | None = None, size: int = 0) -> None:
        self.name = name
        self.size = size


class FakePhotoSize:
    def __init__(self, w: int, h: int) -> None:
        self.w = w
        self.h = h


class FakeStrippedSize:
    """A stripped size carries bytes but no dimensions."""


class FakePhoto:
    def __init__(self, *, id: int = 11, sizes=()) -> None:
        self.id = id
        self.sizes = list(sizes)


class FakeChat:
    def __init__(self, title: str | None = None, username: str | None = None) -> None:
        self.title = title
        self.username = username


class FakeMessage:
    def __init__(
        self,
        *,
        id: int = 10,
        chat_id: int = -100123,
        message: str = "",
        entities=(),
        document=None,
        photo=None,
        file=None,
        grouped_id=None,
        reply_to_msg_id=None,
        sender_id=55,
    ) -> None:
        self.id = id
        self.chat_id = chat_id
        self.message = message
        self.entities = list(entities)
        self.document = document
        self.photo = photo
        self.file = file
        self.grouped_id = grouped_id
        self.reply_to_msg_id = reply_to_msg_id
        self.sender_id = sender_id
        self.date = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        self.chat = FakeChat(title="Fixture Channel")


def test_an_archive_document_becomes_a_downloadable_attachment() -> None:
    parsed = parse_user_message(
        FakeMessage(
            message="A Book",
            document=FakeDocument(),
            file=FakeFile(name="book.zip", size=4096),
        )
    )

    assert parsed is not None
    assert parsed.chat_id == -100123
    assert parsed.message_id == 10
    assert parsed.sender_id == 55
    assert parsed.message_date == "2026-09-29T12:00:00+00:00"
    assert parsed.title == "A Book"
    assert parsed.title_source == "TELEGRAM"
    attachment = parsed.attachments[0]
    assert attachment["type"] == "archive"
    assert attachment["file_name"] == "book.zip"
    assert attachment["size_bytes"] == 4096
    # The coordinates the MTProto download re-reads the message by.
    assert attachment["chat_id"] == -100123
    assert attachment["message_id"] == 10
    # And the field that keeps the Bot API route away from a file it could not
    # resolve: these ids only exist for attachments the bot reported.
    assert attachment["file_id"] == ""
    assert attachment["file_unique_id"] == "mtproto:7"
    assert parsed.file_unique_id == "mtproto:7"


def test_a_document_that_is_not_an_archive_is_not_a_candidate() -> None:
    parsed = parse_user_message(
        FakeMessage(
            message="just a note",
            document=FakeDocument(mime="text/plain"),
            file=FakeFile(name="notes.txt", size=10),
        )
    )

    assert parsed is None


def test_a_photo_uses_its_largest_size() -> None:
    parsed = parse_user_message(
        FakeMessage(
            photo=FakePhoto(
                id=21,
                sizes=(FakeStrippedSize(), FakePhotoSize(90, 120), FakePhotoSize(720, 960)),
            ),
            file=FakeFile(size=2048),
        )
    )

    assert parsed is not None
    attachment = parsed.attachments[0]
    assert attachment["type"] == "photo"
    assert (attachment["width"], attachment["height"]) == (720, 960)
    assert attachment["size_bytes"] == 2048
    assert attachment["file_unique_id"] == "mtproto:21"
    assert parsed.filter_reason == "包含图片预览"


def test_a_hyperlinked_gallery_is_read_off_the_entity() -> None:
    parsed = parse_user_message(
        FakeMessage(
            message="看这个",
            entities=(
                FakeBareEntity(),
                FakeEntity("https://exhentai.org/g/3893499/4f732d0bde/"),
            ),
        )
    )

    assert parsed is not None
    assert parsed.ex_gid == 3893499
    assert parsed.ex_gallery_token == "4f732d0bde"
    assert parsed.title == "看这个"
    assert parsed.filter_reason == "包含 ExHentai 画廊链接"


def test_a_bare_gallery_url_in_the_text_is_found_too() -> None:
    parsed = parse_user_message(
        FakeMessage(message="https://exhentai.org/g/3893499/4f732d0bde/")
    )

    assert parsed is not None
    assert parsed.ex_gid == 3893499


def test_a_preview_link_is_read_off_the_entity() -> None:
    parsed = parse_user_message(
        FakeMessage(
            message="preview",
            entities=(FakeEntity("https://telegra.ph/Some-Book-01-01"),),
        )
    )

    assert parsed is not None
    assert parsed.preview_urls == ("https://telegra.ph/Some-Book-01-01",)
    assert parsed.filter_reason == "包含预览页链接"


def test_a_message_with_nothing_to_review_is_ignored() -> None:
    assert parse_user_message(FakeMessage(message="hello there")) is None


def test_grouping_and_reply_fields_survive_the_translation() -> None:
    parsed = parse_user_message(
        FakeMessage(
            message="caption",
            document=FakeDocument(),
            file=FakeFile(name="book.zip", size=1),
            grouped_id=900,
            reply_to_msg_id=4,
        )
    )

    assert parsed is not None
    assert parsed.media_group_id == "900"
    assert parsed.reply_to_message_id == 4
    assert parsed.is_edit is False
