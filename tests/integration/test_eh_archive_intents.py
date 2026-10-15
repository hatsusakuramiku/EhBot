"""Paid requests survive cancellation/restart without blind resubmission."""
import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.downloads.service import DownloadService
from app.exhentai.archive import ArchiveRequests
from app.exhentai.downloader import ExHentaiDownloader, ExHentaiDownloadError
from app.exhentai.service import ExHentaiService
from tests.integration.test_queue_control import observed
from app.wiring import _exhentai_credentials
from tests.integration.test_eh_account_rotation import accounts


GALLERY = "https://exhentai.org/g/123/fixturetoken/"
ENTRY = "https://exhentai.org/archiver.php?gid=123&token=fixturetoken&or=1"
CONTINUE = "https://archive.example/fixture-session"
FILE = "https://archive.example/fixture.zip"
GALLERY_HTML = '<a href="' + ENTRY.replace("&", "&amp;") + '">Archive Download</a>'
FORM = '''<form method="post"><input type="hidden" name="dltype" value="org">
<div><input type="submit" name="dlcheck" value="Download Original Archive"></div></form>'''
READY = '<p id="continue"><a href="' + CONTINUE + '">Click Here To Start Downloading</a></p>'
LANDING = '<div id="db"><p><a href="/fixture.zip">Download</a></p></div>'
ZIP = b"PK\x03\x04" + b"\x00" * 256


def coordinator(db, service):
    return ArchiveRequests(db, service.registry._secrets)


async def request_archive(db, service, client, account_id, *, requests=None):
    async with service.use_kind("EH", account_id=account_id) as context:
        credentials = await _exhentai_credentials(service)
        return await (requests or coordinator(db, service)).acquire(
            ExHentaiDownloader(client), credentials, context, 123, "fixturetoken")


def outcomes(db):
    with db.connection() as connection:
        return connection.execute("SELECT status, error_code FROM eh_archive_intents ORDER BY created_at, intent_id").fetchall()


@pytest.mark.asyncio
async def test_real_queue_resolves_paid_form_landing_and_attempt_artifact(accounts, tmp_path):
    db, service, ids = accounts
    calls = []
    first_account = (await service.selected_account("EH"))["account_id"]

    async def handler(request):
        calls.append((request.method, str(request.url)))
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if str(request.url) == ENTRY:
            assert request.headers["Cookie"]
            if request.method == "POST":
                assert parse_qs(request.content.decode()) == {"dltype": ["org"], "dlcheck": ["Download Original Archive"]}
                with db.connection() as connection:
                    assert connection.execute("SELECT status, account_id, credential_revision, attempt_id "
                        "FROM eh_archive_intents").fetchone() == ("REQUESTED", first_account, 1,
                        connection.execute("SELECT current_attempt_id FROM queue_tasks").fetchone()[0])
                return httpx.Response(200, text=READY)
            return httpx.Response(200, text=FORM)
        assert request.headers.get("Cookie", "") == ""
        return httpx.Response(200, text=LANDING) if str(request.url) == CONTINUE else httpx.Response(200, content=ZIP)

    with db.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status, ex_gid, ex_gallery_token) VALUES(1, 'APPROVED', 123, 'fixturetoken')")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), cookies={"unrelated": "fixture"}) as client:
        gallery = ExHentaiService(db, tmp_path / "work", tmp_path / "library", lambda: _exhentai_credentials(service),
            http_client=client, account_scope=service.use_kind, archive_requests=coordinator(db, service))
        queue = DownloadService(db, tmp_path, account_scope=service.use_kind, exhentai_download=gallery.download_archive_for_candidate)
        job = await queue.enqueue_exhentai_download(1)
        assert await queue._process_one()
    assert calls == [("GET", GALLERY), ("GET", ENTRY), ("POST", ENTRY), ("GET", CONTINUE), ("GET", FILE)]
    assert outcomes(db) == [("READY", None)]
    task = await queue.get_task(job.task_id)
    assert task["status"] == "COMPLETED"
    with db.connection() as connection:
        path = connection.execute("SELECT path FROM artifacts").fetchone()[0]
        assert task["current_attempt_id"] in path
        assert "fixturetoken" not in connection.execute("SELECT sql FROM sqlite_master WHERE name = 'eh_archive_intents'").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_after_timeout", [False, True])
async def test_transport_timeout_reads_back_once_without_repeating_post(accounts, ready_after_timeout):
    db, service, ids = accounts
    calls = []
    posted = False

    async def handler(request):
        nonlocal posted
        calls.append(request.method)
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            posted = True
            raise httpx.ReadTimeout("controlled lost response", request=request)
        return httpx.Response(200, text=READY if posted and ready_after_timeout else FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if ready_after_timeout:
            assert await request_archive(db, service, client, ids[0]) == CONTINUE
            assert outcomes(db) == [("READY", None)]
        else:
            for account_id in (ids[0], ids[0], ids[1]):
                with pytest.raises(ExHentaiDownloadError) as raised:
                    await request_archive(db, service, client, account_id)
                assert raised.value.code == "EH_ARCHIVE_RESULT_UNKNOWN"
            assert outcomes(db) == [("UNKNOWN", "EH_ARCHIVE_RESULT_UNKNOWN")]
    assert calls.count("POST") == 1
    assert calls == (["GET", "GET", "POST", "GET"] if ready_after_timeout else ["GET", "GET", "POST", "GET", "GET"])


@pytest.mark.asyncio
async def test_unknown_can_be_reconciled_by_fresh_coordinator_with_original_account(accounts):
    db, service, ids = accounts
    posted, ready = False, False
    posts = []

    async def handler(request):
        nonlocal posted
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            posted = True
            posts.append(request)
            return httpx.Response(200, text="<p>Preparing archive</p>")
        return httpx.Response(200, text=READY if posted and ready else FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError):
            await request_archive(db, service, client, ids[0])
        ready = True
        assert await request_archive(db, service, client, ids[0], requests=coordinator(db, service)) == CONTINUE
    assert len(posts) == 1 and outcomes(db) == [("READY", None)]
    with db.connection() as connection:
        columns = [entry[1] for entry in connection.execute("PRAGMA table_info(eh_archive_intents)")]
        serialized = json.dumps(dict(zip(columns, connection.execute("SELECT * FROM eh_archive_intents").fetchone())))
    assert "fixturetoken" not in serialized and CONTINUE not in serialized


@pytest.mark.asyncio
async def test_explicit_gp_refusal_only_reduces_archive_capability(accounts):
    db, service, ids = accounts

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        return httpx.Response(200, text="You do not have enough funds to download this archive. Obtain some Credits or GP and try again."
                              if request.method == "POST" else FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as raised:
            await request_archive(db, service, client, ids[0])
    assert raised.value.code == "EH_GP_INSUFFICIENT"
    assert outcomes(db) == [("REJECTED", "EH_GP_INSUFFICIENT")]
    assert await service.select_account("EH", candidate_versions={ids[0]: 1}, capability="ARCHIVE") is None
    assert await service.select_account("EH", candidate_versions={ids[0]: 1}, capability="METADATA") is not None


@pytest.mark.asyncio
async def test_cancelled_paid_request_retains_unknown_after_actual_cleanup(accounts):
    db, service, ids = accounts
    entered, cleaning, release = (asyncio.Event() for _ in range(3))

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        return httpx.Response(200, text=FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(request_archive(db, service, client, ids[0]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 3)
            assert not task.done() and service.runtime._leases
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert outcomes(db) == [("UNKNOWN", "EH_ARCHIVE_RESULT_UNKNOWN")]
        assert not service.runtime._leases
        with pytest.raises(ExHentaiDownloadError) as raised:
            await request_archive(db, service, client, ids[1])
        assert raised.value.code == "EH_ARCHIVE_RESULT_UNKNOWN"


@pytest.mark.asyncio
async def test_post_503_must_not_be_classified_as_a_definite_rejection(accounts):
    db, service, ids = accounts

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        return httpx.Response(503) if request.method == "POST" else httpx.Response(200, text=FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as raised:
            await request_archive(db, service, client, ids[0])
    assert raised.value.code == "EH_ARCHIVE_RESULT_UNKNOWN"
    assert outcomes(db) == [("UNKNOWN", "EH_ARCHIVE_RESULT_UNKNOWN")]


@pytest.mark.asyncio
async def test_no_recognized_original_form_makes_no_paid_request(accounts):
    db, service, ids = accounts
    posts = []

    async def handler(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200, text=GALLERY_HTML if str(request.url) == GALLERY else "<p>unrecognized</p>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as raised:
            await request_archive(db, service, client, ids[0])
    assert raised.value.code == "EXHENTAI_ARCHIVE_FORM" and posts == []
    assert outcomes(db) == [("REJECTED", "EXHENTAI_ARCHIVE_FORM")]


@pytest.mark.asyncio
async def test_concurrent_accounts_cannot_submit_two_paid_requests_for_one_gid(accounts):
    db, service, ids = accounts
    entered, release = asyncio.Event(), asyncio.Event()
    posts = []

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            posts.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, text=READY)
        return httpx.Response(200, text=FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = asyncio.create_task(request_archive(db, service, client, ids[0]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(ExHentaiDownloadError) as blocked:
                await request_archive(db, service, client, ids[1])
            assert blocked.value.code == "EH_ARCHIVE_RESULT_UNKNOWN"
        finally:
            release.set()
            assert await first == CONTINUE
    assert len(posts) == 1 and outcomes(db) == [("READY", None)]


@pytest.mark.asyncio
async def test_readback_rate_limit_keeps_unknown_fee_and_shared_backoff(accounts):
    db, service, ids = accounts
    posted = False

    async def handler(request):
        nonlocal posted
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            posted = True
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(429, headers={"Retry-After": "120"}) if posted else httpx.Response(200, text=FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as unknown:
            await request_archive(db, service, client, ids[0])
    assert unknown.value.code == "EH_ARCHIVE_RESULT_UNKNOWN"
    assert outcomes(db) == [("UNKNOWN", "EH_ARCHIVE_RESULT_UNKNOWN")]
    with db.connection() as connection:
        assert connection.execute("SELECT scope, error_code FROM eh_shared_backoff").fetchall() == [("SITE", "EXHENTAI_RATE_LIMITED")]
        assert connection.execute("SELECT count(*) FROM eh_account_health").fetchone() == (0,)


@pytest.mark.asyncio
async def test_private_locator_write_failure_cannot_enable_another_paid_submission(accounts, monkeypatch):
    db, service, ids = accounts
    store = service.registry._secrets
    original_write = store.write
    writes, posts = 0, 0

    def failing_write(name, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("controlled private write failure")
        original_write(name, value)

    async def handler(request):
        nonlocal posts
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, text=READY)
        return httpx.Response(200, text=READY if posts else FORM)

    monkeypatch.setattr(store, "write", failing_write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as unknown:
            await request_archive(db, service, client, ids[0])
        assert unknown.value.code == "EH_ARCHIVE_RESULT_UNKNOWN"
        assert outcomes(db) == [("UNKNOWN", "EH_ARCHIVE_RESULT_UNKNOWN")]
        assert await request_archive(db, service, client, ids[0]) == CONTINUE
    assert posts == 1 and outcomes(db) == [("READY", None)]


@pytest.mark.asyncio
async def test_queue_cancellation_preserves_paid_intent_outside_revoked_producer_fence(accounts, tmp_path):
    db, service, _ = accounts
    entered, cleaning, release = (asyncio.Event() for _ in range(3))

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        return httpx.Response(200, text=FORM)

    with db.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status, ex_gid, ex_gallery_token) VALUES(1, 'APPROVED', 123, 'fixturetoken')")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = ExHentaiService(db, tmp_path, tmp_path, lambda: _exhentai_credentials(service), http_client=client,
            account_scope=service.use_kind, archive_requests=coordinator(db, service))
        queue = DownloadService(db, tmp_path, account_scope=service.use_kind, exhentai_download=gallery.download_archive_for_candidate)
        job = await queue.enqueue_exhentai_download(1)
        worker = asyncio.create_task(queue._process_one())
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await queue.command(job.job_id, "cancel", **observed(db, job.job_id))
            stopping = asyncio.create_task(queue.process_operations())
            await asyncio.wait_for(cleaning.wait(), 3)
            assert not stopping.done()
            assert (await queue.get_task(job.task_id))["status"] == "STOPPING"
        finally:
            release.set()
            await worker
            if stopping is not None:
                await stopping
    assert (await queue.get_task(job.task_id))["status"] == "CANCELLED"
    assert outcomes(db) == [("UNKNOWN", "EH_ARCHIVE_RESULT_UNKNOWN")]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)
