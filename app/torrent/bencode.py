"""Minimal bencode reader used to verify a `.torrent` before it is pushed.

Only what a torrent file needs is implemented, and it is implemented strictly:
a lenient parser would let a malformed file through, and the infohash computed
from a misparsed dictionary would not match the one gdata published, which is
the check this module exists to support.
"""

from __future__ import annotations

import hashlib

from app.torrent.models import TorrentError


#: A torrent's metainfo is small. Anything larger is not a torrent, and a cap
#: keeps a hostile payload from being parsed at all.
MAX_TORRENT_BYTES = 4 * 1024 * 1024


def _fail(message: str) -> TorrentError:
    return TorrentError("TORRENT_FILE_INVALID", message)


def _decode(data: bytes, index: int):
    if index >= len(data):
        raise _fail("torrent 数据在解析中意外结束")
    marker = data[index : index + 1]
    if marker == b"i":
        end = data.find(b"e", index)
        if end == -1:
            raise _fail("torrent 整数缺少结束符")
        raw = data[index + 1 : end]
        try:
            # Reject `i-0e` and leading zeros, which bencode forbids and which
            # a re-encoder would normalize, changing the infohash.
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _fail("torrent 整数不是 ASCII") from exc
        if text in {"", "-", "-0"} or (
            text.lstrip("-").startswith("0") and text.lstrip("-") != "0"
        ):
            raise _fail(f"torrent 整数格式非法: {text}")
        return int(text), end + 1
    if marker == b"l":
        items: list = []
        index += 1
        while data[index : index + 1] != b"e":
            value, index = _decode(data, index)
            items.append(value)
        return items, index + 1
    if marker == b"d":
        result: dict[bytes, object] = {}
        index += 1
        previous: bytes | None = None
        while data[index : index + 1] != b"e":
            key, index = _decode(data, index)
            if not isinstance(key, bytes):
                raise _fail("torrent 字典键不是字符串")
            if previous is not None and key <= previous:
                raise _fail("torrent 字典键未按字典序排列")
            previous = key
            value, index = _decode(data, index)
            result[key] = value
        return result, index + 1
    if marker.isdigit():
        separator = data.find(b":", index)
        if separator == -1:
            raise _fail("torrent 字符串缺少长度分隔符")
        try:
            length = int(data[index:separator])
        except ValueError as exc:
            raise _fail("torrent 字符串长度非法") from exc
        start = separator + 1
        end = start + length
        if length < 0 or end > len(data):
            raise _fail("torrent 字符串长度超出数据范围")
        return data[start:end], end
    raise _fail(f"torrent 出现无法识别的标记: {marker!r}")


def decode(data: bytes):
    """Decode a complete bencode document, rejecting trailing bytes."""
    if not data:
        raise _fail("torrent 文件为空")
    if len(data) > MAX_TORRENT_BYTES:
        raise _fail("torrent 文件超过大小上限")
    value, index = _decode(data, 0)
    if index != len(data):
        raise _fail("torrent 文件末尾有多余数据")
    return value


def encode(value) -> bytes:
    """Re-encode a decoded value, used to hash the `info` dictionary."""
    if isinstance(value, bool):
        raise _fail("bencode 不支持布尔值")
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, bytes):
                raise _fail("bencode 字典键必须是字符串")
            parts.append(encode(key) + encode(value[key]))
        return b"d" + b"".join(parts) + b"e"
    raise _fail(f"bencode 不支持的类型: {type(value)!r}")


def infohash(data: bytes) -> str:
    """Compute the v1 infohash of a `.torrent` payload.

    The hash is taken over the re-encoded `info` dictionary. Because the
    decoder rejects unsorted keys and non-canonical integers, a file that
    round-trips here is canonical, so the digest matches what a client will
    compute rather than a value only this code agrees with.
    """
    document = decode(data)
    if not isinstance(document, dict):
        raise _fail("torrent 顶层不是字典")
    info = document.get(b"info")
    if not isinstance(info, dict):
        raise _fail("torrent 缺少 info 字典")
    return hashlib.sha1(encode(info)).hexdigest()


def announce_urls(data: bytes) -> tuple[str, ...]:
    """List the trackers a torrent announces to.

    Used only to assert that the passkey-bearing announce URL never leaves the
    work directory; the value is never logged or persisted.
    """
    document = decode(data)
    if not isinstance(document, dict):
        return ()
    found: list[str] = []
    primary = document.get(b"announce")
    if isinstance(primary, bytes):
        found.append(primary.decode("utf-8", "replace"))
    tiers = document.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, list):
                continue
            for item in tier:
                if isinstance(item, bytes):
                    url = item.decode("utf-8", "replace")
                    if url not in found:
                        found.append(url)
    return tuple(found)


__all__ = [
    "MAX_TORRENT_BYTES",
    "announce_urls",
    "decode",
    "encode",
    "infohash",
]