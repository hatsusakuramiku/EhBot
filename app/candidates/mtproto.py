"""Read a channel the way the user account sees it, in the ingestor's shape.

The Bot API and MTProto describe the same message differently, and the whole
review pipeline is written against the former: `CandidateIngestor` parses a Bot
API update dict, and every rule, filter and download route reads the
`ParsedSourceMessage` that falls out of it. A deployment whose only credential is
a user account has no updates to parse, so this module translates a Telethon
message into that same structure -- one message in, one `ParsedSourceMessage`
out, no database access and no decisions of its own.

Two differences are worth stating outright, because they show up as fields:

* **There is no bot `file_id`.** Those ids are minted by the Bot API and cannot
  be resolved over MTProto, so an attachment ingested here carries an empty
  `file_id` and the `(chat_id, message_id)` coordinates instead. Routing reads
  that: the Telegram route requires a `file_id`, so it will not offer a file it
  could never fetch, and the user-account route needs exactly the coordinates.
* **`file_unique_id` is namespaced** (`mtproto:<id>`) so it can never collide
  with a Bot API unique id. The ingestor uses that value to tell one attachment
  from another, and two providers describing the same file must not look like
  one attachment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.candidates import links
from app.candidates.models import ParsedSourceMessage

#: Suffixes the ingestor treats as a downloadable archive. Kept as a set here
#: rather than imported from the ingestor because the two paths are allowed to
#: disagree about what an archive is without breaking each other -- but they do
#: not, and a test pins them together.
ARCHIVE_SUFFIXES = frozenset({".zip", ".rar", ".7z", ".cbz"})


def parse_user_message(message: Any) -> ParsedSourceMessage | None:
    """Translate one Telethon message, or None when it carries no candidate.

    None is the same answer the Bot API parser gives for a message that offers
    nothing to review: no archive, no gallery link, no preview page and no
    image. The caller records nothing for it.
    """
    chat_id = _int(getattr(message, "chat_id", None))
    message_id = _int(getattr(message, "id", None))
    if chat_id is None or message_id is None:
        return None
    text = str(getattr(message, "message", "") or "").strip()
    urls = links.message_urls({"entities": _text_link_entities(message)}, text)
    gallery_ref = links.find_gallery_ref(urls, text)
    page_urls = links.preview_urls(urls)
    attachments = _attachments(message, chat_id, message_id)
    if not attachments and gallery_ref is None and not page_urls:
        return None

    explicit_title = next(
        (
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().lower().startswith("http")
        ),
        None,
    )
    ex_gid = gallery_ref[0] if gallery_ref is not None else None
    title = explicit_title
    title_source = "TELEGRAM" if explicit_title is not None else None
    title_confidence = 0.9 if explicit_title is not None else None
    if title is None and ex_gid is not None:
        title = f"ExHentai #{ex_gid}"
        title_source = "INFERRED"
        title_confidence = 0.2
    if title is None and attachments:
        archive_name = str(attachments[0].get("file_name") or "")
        if archive_name:
            title = Path(archive_name).stem
            title_source = "FILENAME"
            title_confidence = 0.5

    chat_title = _chat_title(message, chat_id)
    file_unique_id = (
        str(attachments[0].get("file_unique_id") or "") or None
        if attachments
        else None
    )
    if attachments and attachments[0]["type"] == "archive":
        filter_reason = "包含压缩包附件"
    elif attachments:
        filter_reason = "包含图片预览"
    elif gallery_ref is not None:
        filter_reason = "包含 ExHentai 画廊链接"
    else:
        filter_reason = "包含预览页链接"
    return ParsedSourceMessage(
        is_edit=False,
        chat_id=chat_id,
        chat_title=chat_title,
        message_id=message_id,
        sender_id=_int(getattr(message, "sender_id", None)),
        reply_to_message_id=_reply_to_id(message),
        media_group_id=(
            str(getattr(message, "grouped_id"))
            if getattr(message, "grouped_id", None) is not None
            else None
        ),
        message_text=text,
        attachments=attachments,
        file_unique_id=file_unique_id,
        message_date=_message_date(message),
        title=title,
        title_source=title_source,
        title_confidence=title_confidence,
        filter_result="ACCEPT",
        filter_reason=filter_reason,
        ex_gid=ex_gid,
        ex_gallery_token=gallery_ref[1] if gallery_ref is not None else None,
        preview_urls=page_urls,
    )


def _attachments(message: Any, chat_id: int, message_id: int) -> tuple[dict, ...]:
    """The one attachment the pipeline cares about, archive before image.

    One, not a list: the Bot API parser makes the same choice, because a message
    carries either a document or a photo, and the review pipeline's rules are
    written against "the attachment" rather than against a set.
    """
    document = getattr(message, "document", None)
    if document is not None:
        name = _document_name(message)
        if Path(name).suffix.lower() in ARCHIVE_SUFFIXES:
            return (
                {
                    "type": "archive",
                    # Deliberately empty: see the module docstring. Routing
                    # treats a missing file id as 「the Bot API cannot fetch
                    # this」, which is exactly true here.
                    "file_id": "",
                    "file_unique_id": f"mtproto:{_int(getattr(document, 'id', None)) or 0}",
                    "file_name": name,
                    "mime_type": str(getattr(document, "mime_type", "") or ""),
                    "size_bytes": _int(getattr(document, "size", None)) or 0,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
    photo = getattr(message, "photo", None)
    if photo is not None:
        width, height = _photo_dimensions(photo)
        return (
            {
                "type": "photo",
                "file_id": "",
                "file_unique_id": f"mtproto:{_int(getattr(photo, 'id', None)) or 0}",
                "width": width,
                "height": height,
                "size_bytes": _int(
                    getattr(getattr(message, "file", None), "size", None)
                )
                or 0,
            },
        )
    return ()


def _text_link_entities(message: Any) -> list[dict]:
    """The hyperlink targets, in the Bot API entity shape `links` reads.

    Only `text_link` matters: a bare URL in the caption is found by the text
    regex, and MTProto's other entity classes (bold, mention, …) carry no target
    the pipeline looks for. Telethon's `MessageEntityTextUrl` is the one class
    with a `.url`, so testing for the attribute is the whole conversion.
    """
    found: list[dict] = []
    for entity in getattr(message, "entities", None) or ():
        url = getattr(entity, "url", None)
        if url:
            found.append({"type": "text_link", "url": str(url)})
    return found


def _document_name(message: Any) -> str:
    """The uploaded filename, or an empty string.

    Telethon resolves `DocumentAttributeFilename` and falls back to guessing
    from the mime type; a document it cannot name stays empty, which the caller
    reads as 「not an archive」 rather than inventing a name that would be wrong
    in the library.
    """
    name = getattr(getattr(message, "file", None), "name", None)
    return str(name or "")


def _photo_dimensions(photo: Any) -> tuple[int, int]:
    """The largest listed size's dimensions, or (0, 0).

    Largest rather than first: the thumbnail sizes come first, and a rule that
    filtered on image size would otherwise judge every photo by its smallest
    preview. A stripped size carries no dimensions at all, which is why the
    comparison is on the product rather than on 0-width.
    """
    width = height = 0
    for size in getattr(photo, "sizes", None) or ():
        candidate_w = _int(getattr(size, "w", None)) or 0
        candidate_h = _int(getattr(size, "h", None)) or 0
        if candidate_w * candidate_h > width * height:
            width, height = candidate_w, candidate_h
    return width, height


def _reply_to_id(message: Any) -> int | None:
    """The id this message replies to, across Telethon's two spellings.

    Newer versions expose `reply_to.reply_to_msg_id`; older ones only have
    `reply_to_msg_id`. Both are read because the reply chain is what groups a
    caption-less follow-up post with the book it belongs to.
    """
    direct = _int(getattr(message, "reply_to_msg_id", None))
    if direct is not None:
        return direct
    reply = getattr(message, "reply_to", None)
    return _int(getattr(reply, "reply_to_msg_id", None))


def _chat_title(message: Any, chat_id: int) -> str:
    chat = getattr(message, "chat", None)
    for attribute in ("title", "username"):
        value = getattr(chat, attribute, None)
        if value:
            return str(value)
    return str(chat_id)


def _message_date(message: Any) -> str:
    date = getattr(message, "date", None)
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return date.astimezone(UTC).isoformat()
    return datetime.now(tz=UTC).isoformat()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
