"""The worker that reads a new candidate's gallery without being asked (R62).

Ingestion no longer reads a name out of the caption, so a candidate waits in
待补充 until its gallery is fetched. Enrichment used to fire only when an
operator opened a page, the approval sweeper ran, or a pack started -- which
left a candidate nobody looked at sitting there with the answer one HTTP call
away. This worker closes that gap; these tests drive `scrape_once` directly
rather than waiting on its interval.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database
from app.exhentai.scrape_worker import MetadataScrapeWorker
from tests.ingest_admission import permit_all_message_types


async def _seed(database: Database, *, gallery: bool = True) -> int:
    await database.initialize()
    await permit_all_message_types(database)
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Scrape Channel",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    post: dict = {
        "message_id": 1,
        "date": 1_700_000_000,
        "chat": {"id": -100123, "title": "Scrape Channel"},
        "caption": "Caption Guess",
    }
    if gallery:
        post["caption"] = "Caption Guess\nhttps://exhentai.org/g/4242/fixtureTok/"
    else:
        post["photo"] = [
            {
                "file_id": "p",
                "file_unique_id": "pu",
                "width": 800,
                "height": 1200,
            }
        ]
    await database.save_telegram_updates([{"update_id": 500, "channel_post": post}])
    await CandidateIngestor(database).process_pending_updates()
    return (await database.list_candidates(status="NEEDS_INFO"))[0].candidate_id


async def _status(database: Database, candidate_id: int) -> str:
    detail = await database.get_candidate(candidate_id)
    assert detail is not None
    return detail.status


def _naming_scrape(database: Database, calls: list[tuple[int, ...]]):
    """A scrape that behaves like a gallery answer: it names the work."""

    async def scrape(candidate_ids) -> int:
        ids = tuple(int(candidate_id) for candidate_id in candidate_ids)
        calls.append(ids)
        for candidate_id in ids:
            await database.set_candidate_metadata_value(
                candidate_id, "Title", f"画廊标题 {candidate_id}", "EXHENTAI", 0.6
            )
            await database.re_evaluate_candidate_metadata_rules(candidate_id)
        return len(ids)

    return scrape


@pytest.mark.asyncio
async def test_a_new_candidate_is_read_and_leaves_needs_info(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await _seed(database)
    calls: list[tuple[int, ...]] = []
    worker = MetadataScrapeWorker(database, scrape=_naming_scrape(database, calls))

    assert await worker.scrape_once() == 1

    assert calls == [(candidate_id,)]
    assert await _status(database, candidate_id) == "PENDING_REVIEW"
    # Read once: the EXHENTAI row means the next pass has nothing to do.
    assert await worker.scrape_once() == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_candidate_without_a_gallery_is_never_offered(tmp_path: Path) -> None:
    """No gallery means no fetch, and no fetch means the retry backoff is not
    what keeps it out -- it was never a candidate for a scrape at all."""
    database = Database(tmp_path / "ehbot.db")
    await _seed(database, gallery=False)
    calls: list[tuple[int, ...]] = []
    worker = MetadataScrapeWorker(database, scrape=_naming_scrape(database, calls))

    assert await worker.scrape_once() == 0
    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_scrape_is_backed_off(tmp_path: Path) -> None:
    """A gallery that answers with nothing -- deleted, login-walled, an outage --
    must not be re-offered on every pass, or the loop becomes a hot retry
    against a host that just refused."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await _seed(database)
    calls: list[tuple[int, ...]] = []

    async def refusing_scrape(candidate_ids) -> int:
        calls.append(tuple(int(candidate_id) for candidate_id in candidate_ids))
        return 0

    worker = MetadataScrapeWorker(database, scrape=refusing_scrape)

    assert await worker.scrape_once() == 0
    assert calls == [(candidate_id,)]
    # Well inside the first backoff window (30s), so the same candidate is not
    # offered again yet.
    assert await worker.scrape_once() == 0
    assert len(calls) == 1
    assert await _status(database, candidate_id) == "NEEDS_INFO"


@pytest.mark.asyncio
async def test_a_successful_second_attempt_clears_the_backoff(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await _seed(database)
    attempts = 0

    async def flaky_scrape(candidate_ids) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return 0
        for cid in candidate_ids:
            await database.set_candidate_metadata_value(
                cid, "Title", "画廊标题", "EXHENTAI", 0.6
            )
            await database.re_evaluate_candidate_metadata_rules(cid)
        return len(tuple(candidate_ids))

    worker = MetadataScrapeWorker(database, scrape=flaky_scrape, idle_seconds=0.01)
    assert await worker.scrape_once() == 0
    # Pretend the backoff elapsed; the candidate is offered again and named.
    worker._retry_at.clear()
    assert await worker.scrape_once() == 1
    assert await _status(database, candidate_id) == "PENDING_REVIEW"
    assert candidate_id not in worker._attempts


@pytest.mark.asyncio
async def test_a_gallery_that_answers_without_a_name_still_waits(
    tmp_path: Path,
) -> None:
    """The scrape succeeded -- so it is not retried -- but the work is still
    nameless, so it stays in 待补充 for a human instead of becoming reviewable."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await _seed(database)

    async def tag_only_scrape(candidate_ids) -> int:
        ids = tuple(int(candidate_id) for candidate_id in candidate_ids)
        for cid in ids:
            await database.set_candidate_metadata_value(
                cid, "Category", "Doujinshi", "EXHENTAI", 0.6
            )
            await database.re_evaluate_candidate_metadata_rules(cid)
        return len(ids)

    worker = MetadataScrapeWorker(database, scrape=tag_only_scrape)

    assert await worker.scrape_once() == 1
    assert await _status(database, candidate_id) == "NEEDS_INFO"
    detail = await database.get_candidate(candidate_id)
    assert detail is not None and detail.title is None


@pytest.mark.asyncio
async def test_wake_runs_a_pass_without_waiting_for_the_interval(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await _seed(database)
    scraped = asyncio.Event()

    async def scrape(candidate_ids) -> int:
        scraped.set()
        return 0

    worker = MetadataScrapeWorker(database, scrape=scrape, idle_seconds=30)
    await worker.start()
    try:
        worker.wake()
        await asyncio.wait_for(scraped.wait(), timeout=5)
    finally:
        await worker.stop()
