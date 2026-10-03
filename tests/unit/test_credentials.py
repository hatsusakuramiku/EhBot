"""Unit tests for the credential wire format and the stored record.

The database and the routes are covered in `tests/integration/test_api_auth.py`;
this file pins the pure pieces: how a token is shaped, hashed and compared, and
when `ApiCredential` reports itself expired.
"""

from __future__ import annotations

import pytest

from app.credentials import (
    ACCESS_PREFIX,
    API_KEY_PREFIX,
    KIND_ACCESS,
    KIND_API_KEY,
    KIND_REFRESH,
    REFRESH_PREFIX,
    ApiCredential,
    hash_secret,
    new_token,
    parse_token,
    secret_matches,
)


class TestTokenShape:
    @pytest.mark.parametrize(
        ("kind", "prefix"),
        (
            (KIND_API_KEY, API_KEY_PREFIX),
            (KIND_ACCESS, ACCESS_PREFIX),
            (KIND_REFRESH, REFRESH_PREFIX),
        ),
    )
    def test_mint_round_trips_through_parse(self, kind: str, prefix: str) -> None:
        token, public_id, secret = new_token(kind)
        assert token == f"{public_id}.{secret}"
        assert public_id.startswith(prefix)
        assert len(secret) >= 32
        assert parse_token(token) == (public_id, secret)

    def test_tokens_do_not_collide(self) -> None:
        assert len({new_token(KIND_ACCESS)[0] for _ in range(200)}) == 200

    def test_an_unknown_kind_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            new_token("password")

    @pytest.mark.parametrize(
        "raw",
        (
            None,
            "",
            "   ",
            "plain-secret",
            "ehk_missingsecret",
            "ehx_AAAAAAAA.AAAAAAAAAAAAAAAAAAAAAA",  # unknown prefix
            "ehk_short.AAAAAAAAAAAAAAAAAAAAAA",
            "ehk_AAAAAAAA.short",  # secret too short
            "ehk_AAAAAAAA.AAAA.BBBB",  # two separators
            "ehk_AAAAAAAA.",
        ),
    )
    def test_malformed_shapes_are_rejected(self, raw: str | None) -> None:
        assert parse_token(raw) is None

    def test_whitespace_around_a_valid_token_is_tolerated(self) -> None:
        token, public_id, secret = new_token(KIND_ACCESS)
        assert parse_token(f"  {token}\n") == (public_id, secret)


class TestHashing:
    def test_hash_is_stable_hex(self) -> None:
        digest = hash_secret("a-secret")
        assert digest == hash_secret("a-secret")
        assert len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)

    def test_a_secret_matches_only_its_own_hash(self) -> None:
        assert secret_matches("right", hash_secret("right"))
        assert not secret_matches("wrong", hash_secret("right"))
        assert not secret_matches("right", None)
        assert not secret_matches("right", "")

    def test_an_empty_stored_hash_never_matches(self) -> None:
        assert not secret_matches("", "")


class TestExpiry:
    def test_none_means_never(self) -> None:
        credential = ApiCredential(
            id=1,
            kind=KIND_API_KEY,
            label="mobile",
            public_id=API_KEY_PREFIX + "x",
            secret_hash="h",
            expires_at=None,
        )
        assert not credential.is_expired(now=2_000_000_000)

    def test_the_boundary_is_expired(self) -> None:
        credential = ApiCredential(
            id=1,
            kind=KIND_ACCESS,
            label="",
            public_id=ACCESS_PREFIX + "x",
            secret_hash="h",
            expires_at=1000,
        )
        assert not credential.is_expired(now=999)
        assert credential.is_expired(now=1000)
        assert credential.is_expired(now=1001)

    def test_revoked_is_a_separate_fact_from_expired(self) -> None:
        credential = ApiCredential(
            id=1,
            kind=KIND_ACCESS,
            label="",
            public_id=ACCESS_PREFIX + "x",
            secret_hash="h",
            revoked_at="2026-10-03 00:00:00",
        )
        assert credential.is_revoked
        assert not credential.is_expired(now=2_000_000_000)
