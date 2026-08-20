from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets


KEY_LENGTH = 32
NONCE_LENGTH = 16
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
VERSION = 1


class VaultError(ValueError):
    """Raised when a stored password ciphertext cannot be opened."""


def _derive_key(master_key: bytes, salt: bytes, length: int) -> bytes:
    return hashlib.scrypt(
        master_key,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=length,
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate a deterministic keystream from HMAC-SHA256 in counter mode."""
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks += hmac.new(
            key, nonce + counter.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        counter += 1
    return bytes(blocks[:length])


def encrypt_password(master_key: bytes, plaintext: str) -> str:
    """Encrypt an archive password for storage.

    The service has no third-party crypto dependency, so this uses a
    scrypt-derived key with an HMAC-SHA256 keystream and an HMAC tag over the
    ciphertext. Passwords are only ever stored and compared through this
    envelope; they are never logged or exported.
    """
    if not plaintext:
        raise VaultError("password must not be empty")
    salt = secrets.token_bytes(NONCE_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)
    keys = _derive_key(master_key, salt, KEY_LENGTH * 2)
    cipher_key, mac_key = keys[:KEY_LENGTH], keys[KEY_LENGTH:]
    payload = plaintext.encode("utf-8")
    ciphertext = bytes(
        a ^ b for a, b in zip(payload, _keystream(cipher_key, nonce, len(payload)))
    )
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    envelope = {
        "v": VERSION,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "data": base64.b64encode(ciphertext).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":"))


def decrypt_password(master_key: bytes, envelope_json: str) -> str:
    try:
        envelope = json.loads(envelope_json)
        salt = base64.b64decode(envelope["salt"])
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["data"])
        tag = base64.b64decode(envelope["tag"])
        version = int(envelope["v"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError("stored password envelope is malformed") from exc
    if version != VERSION:
        raise VaultError(f"unsupported password envelope version {version}")
    keys = _derive_key(master_key, salt, KEY_LENGTH * 2)
    cipher_key, mac_key = keys[:KEY_LENGTH], keys[KEY_LENGTH:]
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise VaultError("stored password failed integrity verification")
    plaintext = bytes(
        a ^ b
        for a, b in zip(
            ciphertext, _keystream(cipher_key, nonce, len(ciphertext))
        )
    )
    return plaintext.decode("utf-8")


def generate_master_key() -> bytes:
    return os.urandom(KEY_LENGTH)


__all__ = [
    "VaultError",
    "decrypt_password",
    "encrypt_password",
    "generate_master_key",
]