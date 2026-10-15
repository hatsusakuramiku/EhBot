"""The approved cross-account identity rules, independent of transport spelling."""
import pytest

from app.candidates.identity import ingest_key
from app.candidates.entries import message_key
from app.candidates.ingestor import CandidateIngestor
from app.candidates.mtproto import parse_user_message
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace


def test_same_gallery_in_different_messages_and_accounts_has_one_key():
    assert ingest_key(gallery_id=123456, account_id="bot-a", peer_id=-25, message_id=9) == ingest_key(
        gallery_id=123456, account_id="user-b", peer_id=42, message_id=18) == "eh:gallery:123456"


def test_bot_and_mtproto_channel_observations_match():
    assert ingest_key(channel_id=-1000000000777, message_id=42, account_id="bot-a") == ingest_key(
        channel_id=777, message_id=42, account_id="user-b") == "tg:channel:777:42"


def test_complete_forward_matches_original_and_overrides_receiving_group():
    original = ingest_key(channel_id=777, message_id=42)
    assert ingest_key(origin_channel_id=-1000000000777, origin_message_id=42,
        channel_id=888, message_id=66) == original
    assert ingest_key(origin_channel_id=777, origin_message_id=42,
        account_id="user-b", peer_id=-555, message_id=18) == original


def test_incomplete_forward_uses_received_identity_without_guessing():
    assert ingest_key(origin_channel_id=777, channel_id=888, message_id=66) == "tg:channel:888:66"
    assert ingest_key(origin_message_id=42, account_id="user-b", peer_id=-555, message_id=18) == "tg:account:user-b:peer:-555:message:18"


def test_private_and_ordinary_group_messages_remain_account_scoped():
    keys = {ingest_key(account_id=account, peer_id=peer, message_id=18)
            for account in ("bot-a", "user-b") for peer in (555, -555)}
    assert len(keys) == 4


def test_account_component_cannot_inject_another_key_segment():
    assert ingest_key(account_id="a:peer:2", peer_id=3, message_id=4) == "tg:account:a%3Apeer%3A2:peer:3:message:4"


@pytest.mark.parametrize("values", [
    {}, {"gallery_id": 0}, {"gallery_id": True}, {"gallery_id": "123"},
    {"channel_id": -555, "message_id": 18}, {"channel_id": -1000000000000, "message_id": 18},
    {"channel_id": 777}, {"channel_id": 777, "message_id": False},
    {"account_id": "a", "peer_id": 0, "message_id": 18},
    {"account_id": " a", "peer_id": 555, "message_id": 18},
    {"origin_channel_id": 777, "origin_message_id": -1},
])
def test_unreliable_or_invalid_coordinates_never_create_a_key(values):
    with pytest.raises(ValueError):
        ingest_key(**values)


def bot_message(*, chat=None, forward=None):
    return {"message": {"message_id": 66, "date": 1700000000,
                        "chat": chat or {"id": -1000000000888, "type": "supergroup"},
                        "text": "https://telegra.ph/fixture", "forward_origin": forward}}


def test_real_bot_and_telethon_parsers_use_the_same_complete_forward_identity():
    parsed_bot = CandidateIngestor._parse_message(bot_message(forward={
        "type": "channel", "chat": {"id": -1000000000777, "type": "channel"}, "message_id": 42,
    }))
    parsed_user = parse_user_message(SimpleNamespace(
        chat_id=-20, id=100, message="https://telegra.ph/fixture", date=datetime.now(UTC),
        peer_id=SimpleNamespace(chat_id=20),
        fwd_from=SimpleNamespace(from_id=SimpleNamespace(channel_id=777), channel_post=42),
    ))
    assert message_key(replace(parsed_bot, account_id="bot-a")) == message_key(
        replace(parsed_user, account_id="user-b")) == "tg:channel:777:42"


def test_bot_hidden_origin_does_not_guess_cross_group_equivalence():
    parsed = CandidateIngestor._parse_message(bot_message(chat={"id": -20, "type": "group"},
        forward={"type": "hidden_user", "sender_user_name": "someone", "date": 1700000000}))
    assert parsed.origin_channel_id is None and parsed.origin_message_id is None
    assert message_key(replace(parsed, account_id="bot-a")) == "tg:account:bot-a:peer:-20:message:66"


def test_telethon_incomplete_forward_uses_proven_current_channel():
    parsed = parse_user_message(SimpleNamespace(
        chat_id=-1000000000888, id=66, message="https://telegra.ph/fixture", date=datetime.now(UTC),
        peer_id=SimpleNamespace(channel_id=888),
        fwd_from=SimpleNamespace(from_id=SimpleNamespace(channel_id=777)),
    ))
    assert parsed.origin_channel_id is None and parsed.origin_message_id is None
    assert message_key(parsed) == "tg:channel:888:66"
