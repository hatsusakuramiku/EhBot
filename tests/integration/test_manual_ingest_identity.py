"""Manual and Telegram admission compete on one key before supplier requests."""
import asyncio
from contextlib import asynccontextmanager
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from app.candidates.manual import ingest_manual_link
from app.candidates.ingestor import CandidateIngestor
from app.db.database import DuplicateGalleryCandidate
from app.exhentai.service import ExHentaiService
from app.review.service import ReviewError
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_pipeline import setup, accepted


class Gallery(ExHentaiService):
    def __init__(self, db):
        super().__init__(db, db.path.parent / 'work', db.path.parent / 'library', None)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.persisted = []
        self.error = None
        self.gallery = None

    @asynccontextmanager
    async def account_request(self):
        yield

    async def fetch_metadata_for_gallery(self, gid, token):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return {"Title": "fixture"}, self.gallery

    async def publish_manual_gallery(self, gid, token, metadata, gallery, *, ingest_claim):
        cid = await super().publish_manual_gallery(gid, token, metadata, gallery, ingest_claim=ingest_claim)
        self.persisted.append(cid)
        return cid


async def manual(db, gallery):
    return await ingest_manual_link("https://e-hentai.org/g/123/token/", database=db, exhentai_service=gallery,
        orchestrator=SimpleNamespace(route_source=lambda candidate: SimpleNamespace(provider=None)), download_service=None)


@pytest.mark.asyncio
async def test_manual_owner_blocks_second_manual_and_ai_but_keeps_received_observation(tmp_path):
    db, (account, _) = await setup(tmp_path)
    gallery = Gallery(db)
    first = asyncio.create_task(manual(db, gallery))
    try:
        await asyncio.wait_for(gallery.entered.wait(), 5)
        with pytest.raises(ReviewError) as error:
            await manual(db, gallery)
        assert error.value.code == "INGEST_ALREADY_PROCESSING"
        class Admission:
            async def decide(self, message):
                pytest.fail("paid admission must not race manual validation")
        assert not (await CandidateIngestor(db, admission=Admission()).process_message(message(account)))[0]
        with db.connection() as c:
            assert c.execute("SELECT count(*) FROM candidates").fetchone() == (0,)
            assert c.execute("SELECT count(*) FROM source_messages").fetchone() == (1,)
    finally:
        gallery.release.set()
    cid = await first
    assert gallery.calls == 1 and gallery.persisted == [cid]
    with db.connection() as c:
        assert c.execute("SELECT candidate_id, processing_state FROM ingest_entries").fetchone() == (cid, "LINKED")
        assert c.execute("SELECT candidate_id FROM candidate_messages").fetchall() == [(cid,)]
    with pytest.raises(DuplicateGalleryCandidate):
        await manual(db, gallery)
    assert gallery.calls == 1


@pytest.mark.asyncio
async def test_manual_cannot_race_in_progress_telegram_ai(tmp_path):
    db, (account, _) = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    class Admission:
        async def decide(self, message):
            entered.set()
            await release.wait()
            return accepted()
    first = asyncio.create_task(CandidateIngestor(db, admission=Admission()).process_message(message(account)))
    gallery = Gallery(db)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ReviewError) as error:
            await manual(db, gallery)
        assert error.value.code == "INGEST_ALREADY_PROCESSING" and gallery.calls == 0
    finally:
        release.set()
    assert (await first)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind", ["manual", "telegram"])
async def test_expired_live_manual_or_telegram_owner_blocks_both_admission_paths(tmp_path, first_kind):
    db, (account, _) = await setup(tmp_path)
    gallery = Gallery(db)
    entered, release = asyncio.Event(), asyncio.Event()

    class Admission:
        async def decide(self, parsed):
            entered.set()
            await release.wait()
            return accepted()

    ingestor = CandidateIngestor(db, admission=Admission())
    first = asyncio.create_task(manual(db, gallery) if first_kind == "manual" else ingestor.process_message(message(account)))
    try:
        await asyncio.wait_for((gallery.entered if first_kind == "manual" else entered).wait(), 3)
        with db.connection() as connection:
            connection.execute("UPDATE ingest_entries SET lease_until = unixepoch() - 1")
        with pytest.raises(ReviewError) as error:
            await manual(db, gallery)
        assert error.value.code == "INGEST_ALREADY_PROCESSING"

        class ForbiddenAdmission:
            async def decide(self, parsed):
                pytest.fail("an expired lease must not overlap a locally live provider request")

        assert not (await CandidateIngestor(db, admission=ForbiddenAdmission()).process_message(message(account)))[0]
        assert gallery.calls == (1 if first_kind == "manual" else 0)
        assert not first.done()
    finally:
        first.cancel()
        gallery.release.set()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert db.ingest_processors.active_ids() == ()
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (0,)


@pytest.mark.asyncio
async def test_expired_manual_cleanup_survives_repeated_cancellation_before_replacement(tmp_path, monkeypatch):
    db, (account, _) = await setup(tmp_path)
    gallery = Gallery(db)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fetch(gid, token):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr(gallery, "fetch_metadata_for_gallery", fetch)
    first = asyncio.create_task(manual(db, gallery))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        first.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        with db.connection() as connection:
            connection.execute("UPDATE ingest_entries SET lease_until = unixepoch() - 1")
        assert not (await CandidateIngestor(db).process_message(message(account)))[0]
        with pytest.raises(ReviewError) as error:
            await manual(db, gallery)
        assert error.value.code == "INGEST_ALREADY_PROCESSING"
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done() and db.ingest_processors.active_ids()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert first.cancelled() and not db.ingest_processors.active_ids()
    assert await CandidateIngestor(db).process_pending_entries() == 0  # a different account must not borrow the input
    from app.connections.accounts import AccountRegistry
    from app.connections.lifecycle import AccountLifecycle
    from app.connections.runtime import AccountRuntime
    from app.secrets import SecretStore
    registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
    runtime = AccountRuntime(registry, AccountLifecycle(registry))
    async with runtime.use(account):
        assert await CandidateIngestor(db).process_pending_entries() == 1


@pytest.mark.asyncio
async def test_failed_manual_validation_keeps_recoverable_key_without_partial_candidate(tmp_path):
    db, _ = await setup(tmp_path)
    gallery = Gallery(db)
    gallery.release.set()
    gallery.error = RuntimeError("fixture failure")
    with pytest.raises(RuntimeError, match="fixture failure"):
        await manual(db, gallery)
    with db.connection() as c:
        assert c.execute("SELECT candidate_id, processing_state, owner FROM ingest_entries").fetchone() == (None, "PENDING", None)
        assert c.execute("SELECT count(*) FROM candidates").fetchone() == (0,)
        c.execute("UPDATE ingest_entries SET retry_at = 0")
    gallery.error = None
    assert await manual(db, gallery) > 0
    assert gallery.calls == 2


@pytest.mark.asyncio
async def test_revoked_manual_processor_cannot_create_candidate(tmp_path):
    db, _ = await setup(tmp_path)
    gallery = Gallery(db)
    first = asyncio.create_task(manual(db, gallery))
    try:
        await asyncio.wait_for(gallery.entered.wait(), 5)
        with db.connection() as c:
            c.execute("UPDATE ingest_entries SET processing_state = 'REMOVED', revision = revision + 1, owner = NULL, lease_until = NULL")
    finally:
        gallery.release.set()
    with pytest.raises(ReviewError) as error:
        await first
    assert error.value.code == "INGEST_STALE_PROCESSOR" and not gallery.persisted
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone() == (0,)


@pytest.mark.asyncio
async def test_metadata_failure_rolls_back_candidate_thumbnail_and_linked_owner(tmp_path):
    db, _ = await setup(tmp_path)
    gallery = Gallery(db)
    gallery.release.set()
    gallery.gallery = SimpleNamespace(best_torrent=None, torrent_count=0, thumb='https://fixture.invalid/cover.jpg')
    with db.connection() as c:
        c.execute("CREATE TRIGGER fixture_cover_failure BEFORE INSERT ON thumbnails BEGIN "
                  "SELECT RAISE(ABORT, 'fixture cover failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='fixture cover failure'):
        await manual(db, gallery)
    with db.connection() as c:
        for table in ('candidates', 'metadata_values', 'thumbnails', 'candidate_approvals'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone() == (0,)
        assert c.execute('SELECT processing_state, candidate_id, owner FROM ingest_entries').fetchone() == ('PENDING', None, None)


@pytest.mark.asyncio
async def test_candidate_and_all_validated_metadata_become_visible_together(tmp_path, monkeypatch):
    db, _ = await setup(tmp_path)
    gallery = Gallery(db)
    gallery.release.set()
    gallery.gallery = SimpleNamespace(best_torrent=None, torrent_count=0, thumb='https://fixture.invalid/cover.jpg')
    entered, release = Event(), Event()
    write_cover = gallery._write_cover
    def gated_cover(connection, cid, data):
        entered.set()
        assert release.wait(5), 'test did not release publication'
        write_cover(connection, cid, data)
    monkeypatch.setattr(gallery, '_write_cover', gated_cover)
    task = asyncio.create_task(manual(db, gallery))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with db.connection() as c:
            assert c.execute('SELECT count(*) FROM candidates').fetchone() == (0,)
            assert c.execute('SELECT count(*) FROM metadata_values').fetchone() == (0,)
            assert c.execute('SELECT processing_state FROM ingest_entries').fetchone() == ('PROCESSING',)
    finally:
        release.set()
    cid = await task
    with db.connection() as c:
        assert c.execute('SELECT field_value FROM metadata_values WHERE candidate_id = ?', (cid,)).fetchone() == ('fixture',)
        assert c.execute('SELECT torrent_count, thumb_url FROM candidates WHERE id = ?', (cid,)).fetchone() == (0, gallery.gallery.thumb)
        assert c.execute('SELECT count(*) FROM thumbnails').fetchone() == (1,)
        assert c.execute('SELECT processing_state FROM ingest_entries').fetchone() == ('LINKED',)


@pytest.mark.asyncio
async def test_heartbeat_due_during_publication_cannot_renew_finished_claim(tmp_path, monkeypatch):
    db, _ = await setup(tmp_path)
    gallery = Gallery(db)
    gallery.release.set()
    gallery.gallery = SimpleNamespace(best_torrent=None, torrent_count=0, thumb='https://fixture.invalid/cover.jpg')
    entered, release = Event(), Event()
    due, timer_requested = asyncio.Event(), asyncio.Event()
    original_wait = asyncio.wait_for
    async def timed_wait(awaitable, timeout):
        if timeout != 30:
            return await original_wait(awaitable, timeout)
        try:
            await due.wait()
            timer_requested.set()
            raise TimeoutError
        finally:
            awaitable.close()
    monkeypatch.setattr(asyncio, 'wait_for', timed_wait)
    write_cover = gallery._write_cover
    def gated_cover(connection, cid, data):
        entered.set()
        assert release.wait(5)
        write_cover(connection, cid, data)
    monkeypatch.setattr(gallery, '_write_cover', gated_cover)
    task = asyncio.create_task(manual(db, gallery))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        due.set()
        await original_wait(timer_requested.wait(), 5)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    assert await original_wait(task, 5) > 0
    with db.connection() as c:
        assert c.execute('SELECT processing_state FROM ingest_entries').fetchone() == ('LINKED',)
