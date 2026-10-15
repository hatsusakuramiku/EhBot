"""Canonical ingestion identity shared by imported and newly parsed inputs."""
from urllib.parse import quote


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _channel(value: int) -> int:
    """Accept positive MTProto ids or Bot/Telethon channel-marked ids.

    Marked channel ids are -(1000000000000 + channel_id). Other negative
    peer ids identify ordinary groups and cannot become shared channel keys.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("channel_id must be an integer")
    if value < -1000000000000:
        return -value - 1000000000000
    return _positive(value, "channel_id")


def ingest_key(*, gallery_id: int | None = None,
               origin_channel_id: int | None = None, origin_message_id: int | None = None,
               channel_id: int | None = None, message_id: int | None = None,
               account_id: str | None = None, peer_id: int | None = None,
               legacy_candidate_id: int | None = None) -> str:
    """Return one key for equivalent reliable identities; never guess identity.

    gallery_id is a positive trusted gallery id and takes precedence over all
    message coordinates. A complete origin channel/message pair takes priority
    over the receiving channel. channel_id must be positively identified as a
    channel/supergroup by the transport; a negative peer id alone is insufficient.
    message ids are positive. Ordinary/unknown peers require account_id and a
    nonzero peer_id; positive private peers and negative ordinary groups remain
    scoped to that account. Missing reliable identity raises ValueError.

    Tokens, titles, timestamps, file ids and raw JSON do not participate. The
    caller keeps received coordinates and all account attachments as extra data.
    legacy_candidate_id is a positive historical row id, used only for a work
    with neither gallery nor source coordinates; it makes no equivalence claim.
    """
    if gallery_id is not None:
        return f"eh:gallery:{_positive(gallery_id, 'gallery_id')}"
    if origin_channel_id is not None and origin_message_id is not None:
        return f"tg:channel:{_channel(origin_channel_id)}:{_positive(origin_message_id, 'origin_message_id')}"
    if channel_id is not None:
        return f"tg:channel:{_channel(channel_id)}:{_positive(message_id, 'message_id')}"
    if message_id is None and legacy_candidate_id is not None:
        return f"legacy:candidate:{_positive(legacy_candidate_id, 'legacy_candidate_id')}"
    if not isinstance(account_id, str) or not account_id or account_id != account_id.strip():
        raise ValueError("account-scoped identity requires account_id")
    if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id == 0:
        raise ValueError("account-scoped identity requires a nonzero peer_id")
    return f"tg:account:{quote(account_id, safe='')}:peer:{peer_id}:message:{_positive(message_id, 'message_id')}"
