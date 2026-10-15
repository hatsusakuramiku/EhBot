"""Removed gallery receipts distinguish fresh posts from late copies and edits."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.candidates.ingestor import CandidateIngestor
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_pipeline import setup


async def removed_work(tmp_path):
    db, (a, b) = await setup(tmp_path)
    await CandidateIngestor(db).process_message(message(a))
    with db.connection() as c:
        c.execute("DELETE FROM candidates")
        removed_at = c.execute("SELECT json_extract(extra_json, '$.removed_at') FROM ingest_entries").fetchone()[0]
    fresh = datetime.fromtimestamp(removed_at + 1, UTC).isoformat()
    return db, a, b, fresh


@pytest.mark.asyncio
async def test_genuinely_new_post_reopens_one_key_and_old_account_copies_stay_in_old_generation(tmp_path):
    db, a, b, fresh = await removed_work(tmp_path)
    ingestor = CandidateIngestor(db)
    assert (await ingestor.process_message(replace(message(a, message_id=99), message_date=fresh)))[0]
    assert not (await ingestor.process_message(message(b)))[0]
    assert not (await ingestor.process_message(replace(message(a), is_edit=True, message_text="late edit", message_date=fresh)))[0]
    with db.connection() as c:
        assert c.execute("SELECT count(*), max(generation), max(processing_state) FROM ingest_entries").fetchone() == (1, 2, "LINKED")
        assert c.execute("SELECT sm.message_id, sm.ingest_generation FROM candidate_messages cm "
                         "JOIN source_messages sm ON sm.id = cm.source_message_id").fetchall() == [(99, 2)]
        assert c.execute("SELECT count(*) FROM source_messages WHERE ingest_generation = 1").fetchone() == (2,)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["old_queued", "hidden_forward", "known_forward", "edit", "unknown_peer"])
async def test_old_or_unproven_observation_does_not_resurrect_removed_work(tmp_path, kind):
    db, _, b, fresh = await removed_work(tmp_path)
    incoming = replace(message(b, message_id=88), message_date=fresh)
    if kind == "old_queued":
        incoming = replace(incoming, message_date="2026-10-13T00:00:00+00:00")
    elif kind == "hidden_forward":
        incoming = replace(incoming, is_forwarded=True)
    elif kind == "known_forward":
        incoming = replace(incoming, is_forwarded=True, origin_channel_id=777, origin_message_id=42)
    elif kind == "edit":
        incoming = replace(incoming, is_edit=True)
    else:
        incoming = replace(incoming, peer_type=None)
    assert not (await CandidateIngestor(db).process_message(incoming))[0]
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone() == (0,)
        assert c.execute("SELECT processing_state, generation FROM ingest_entries").fetchone() == ("REMOVED", 1)


@pytest.mark.asyncio
async def test_explicit_manual_readd_does_not_attach_old_sources_or_let_old_edits_move_them(tmp_path):
    db, a, b, fresh = await removed_work(tmp_path)
    cid = await db.create_manual_candidate(filter_reason="fixture explicit re-add", ex_gid=123, ex_gallery_token="new-token")
    await CandidateIngestor(db).process_message(replace(message(a), is_edit=True, message_date=fresh))
    await CandidateIngestor(db).process_message(message(b))
    with db.connection() as c:
        assert c.execute("SELECT candidate_id, generation, processing_state FROM ingest_entries").fetchone() == (cid, 2, "LINKED")
        assert c.execute("SELECT count(*) FROM candidate_messages").fetchone() == (0,)
        assert c.execute("SELECT DISTINCT ingest_generation FROM source_messages").fetchall() == [(1,)]
