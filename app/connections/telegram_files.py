"""Compare archive document identities across Bot API and MTProto observations."""
import base64
import binascii
import re
import struct


def document_id(attachment: dict) -> int | None:
    """Read a document identity, refusing unknown, photo and malformed identifiers.

    TDLib FileNode::get_unique_id encodes FullRemoteFileLocation::AsUnique:
    base64url(zero_encode(int32 document_class=2 + int64 document_id)).
    MTProto ingestion already records that same int64 as mtproto:<id>.
    No Bot file_id, access hash, name or size is used as file identity.
    """
    value = attachment.get("file_unique_id")
    if not isinstance(value, str):
        return None
    if value.startswith("mtproto:"):
        if not re.fullmatch(r"mtproto:-?[0-9]{1,20}", value):
            return None
        identity = int(value[8:])
        return identity if -(2**63) <= identity < 2**63 and identity != 0 else None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        return None
    try:
        encoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return None
    decoded = bytearray()
    index = 0
    while index < len(encoded):
        current = encoded[index]
        index += 1
        if current == 0:
            if index == len(encoded) or encoded[index] == 0:
                return None
            decoded.extend(b"\0" * encoded[index])
            index += 1
        else:
            decoded.append(current)
        if len(decoded) > 12:
            return None
    if len(decoded) != 12:
        return None
    kind, identity = struct.unpack("<iq", decoded)
    return identity if kind == 2 and identity != 0 else None
