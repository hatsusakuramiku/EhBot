"""EH queue attempts use finite account pools and never duplicate unknown fees."""
import asyncio
import json
from http.cookies import SimpleCookie

import httpx
import pytest

from app.downloads.service import DownloadService
from app.exhentai.accounts import DownloadAccountPool
from app.exhentai.service import ExHentaiService
from app.torrent.models import TorrentClientConfig
from app.torrent.service import TorrentService
from app.wiring import _exhentai_credentials
from tests.integration.test_eh_account_rotation import accounts
from tests.integration.test_eh_archive_intents import GALLERY, ENTRY, CONTINUE, FILE, GALLERY_HTML, FORM, READY, LANDING, ZIP, coordinator
from tests.integration.test_queue_control import observed
from tests.integration.test_torrent_workflow import FakeClient, TORRENT_PAYLOAD, TORRENT_DIGEST, TORRENT_PAGE


def member(request):
    cookie = SimpleCookie(request.headers.get("Cookie", ""))
    return cookie["ipb_member_id"].value if "ipb_member_id" in cookie else None


def seed(db):
    with db.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status, ex_gid, ex_gallery_token) VALUES(1, 'APPROVED', 123, 'fixturetoken')")


def queue_for(db, service, client, root):
    gallery = ExHentaiService(db, root / "work", root / "library", lambda: _exhentai_credentials(service),
        http_client=client, account_scope=service.use_kind, archive_requests=coordinator(db, service))
    return DownloadService(db, root / "work", account_scope=service.use_kind,
        exhentai_download=gallery.download_archive_for_candidate, exhentai_source=DownloadAccountPool(service).select)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaust", [False, True])
async def test_explicit_gp_rejection_advances_one_queue_attempt_per_account(accounts, tmp_path, exhaust):
    db, service, ids = accounts
    seed(db)
    posts = []

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if str(request.url) == ENTRY:
            if request.method == "POST":
                posts.append(member(request))
                if exhaust or len(posts) == 1:
                    return httpx.Response(200, text="You do not have enough funds to download this archive.")
                return httpx.Response(200, text=READY)
            return httpx.Response(200, text=FORM)
        assert not request.headers["Cookie"]
        return httpx.Response(200, text=LANDING) if str(request.url) == CONTINUE else httpx.Response(200, content=ZIP)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        queue = queue_for(db, service, client, tmp_path)
        result = await queue.enqueue_exhentai_download(1)
        for _ in ids:
            assert await queue._process_one()
            if exhaust and len(posts) == 1:
                extra = await service.registry.create("EH", "later", {"ipb_member_id": "later", "ipb_pass_hash": "later", "igneous": "later"})
                extra = await service.registry.record_identity(extra["account_id"], extra["credential_revision"], "later", "later")
                extra = await service.registry.enable(extra["account_id"], expected_revision=extra["revision"])
                from app.connections.models import ProviderStatus
                service._states[extra["account_id"]] = ProviderStatus("connected", True)
            await queue.process_operations()
            task = await queue.get_task(result.task_id)
            if task["status"] in {"FAILED", "COMPLETED"}:
                break
            # Rebuild the worker between attempts; the pool is durable.
            queue = queue_for(db, service, client, tmp_path)
        assert task["status"] == ("FAILED" if exhaust else "COMPLETED")
        assert len(posts) == (3 if exhaust else 2) == len(set(posts))
        assert len(task["attempts"]) == len(posts)
        selected = [json.loads(attempt["details_json"])["account_id"] for attempt in task["attempts"]]
        assert len(set(selected)) == len(posts)
        assert all(attempt["finished_at"] for attempt in task["attempts"])
        with db.connection() as connection:
            assert connection.execute("SELECT count(*) FROM eh_archive_intents WHERE status = 'REJECTED'").fetchone() == (len(posts) if exhaust else 1,)
            if exhaust:
                assert connection.execute("SELECT error_code FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("EH_ALL_ACCOUNTS_FAILED",)


@pytest.mark.asyncio
async def test_unknown_charge_retry_reconciles_original_account_without_another_post(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    posts = []
    ready = False
    reads = []

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if str(request.url) == ENTRY:
            if request.method == "POST":
                posts.append(member(request))
                raise httpx.ReadTimeout("fixture-timeout")
            reads.append(member(request))
            return httpx.Response(200, text=READY if ready else FORM)
        return httpx.Response(200, text=LANDING) if str(request.url) == CONTINUE else httpx.Response(200, content=ZIP)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        queue = queue_for(db, service, client, tmp_path)
        result = await queue.enqueue_exhentai_download(1)
        assert await queue._process_one()
        assert (await queue.get_task(result.task_id))["status"] == "FAILED"
        assert await queue.process_operations() == 0
        assert len(posts) == 1
        # Restore only the mock provider's read-only result, not queue state.
        ready = True
        retried = await queue.command(result.job_id, "retry", **observed(db, result.job_id))
        queue = queue_for(db, service, client, tmp_path)
        assert await queue._process_one()
        assert len(posts) == 1 and set(reads) == set(posts)
        assert (await queue.get_task(retried["task_id"]))["status"] == "COMPLETED"
        with db.connection() as connection:
            assert connection.execute("SELECT count(*) FROM eh_archive_intents").fetchone() == (1,)


@pytest.mark.asyncio
async def test_operator_cancel_wins_during_eh_replacement_selection(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    pool = DownloadAccountPool(service)

    async def unavailable(candidate):
        from app.exhentai.errors import ExHentaiDownloadError
        calls.append(candidate)
        raise ExHentaiDownloadError("EH_GP_INSUFFICIENT", "fixture")

    async def selection(*args, **kwargs):
        if calls:
            entered.set()
            await release.wait()
        return await pool.select(*args, **kwargs)

    queue = DownloadService(db, tmp_path, account_scope=service.use_kind, exhentai_download=unavailable, exhentai_source=selection)
    result = await queue.enqueue_exhentai_download(1)
    await queue._process_one()
    pending = asyncio.create_task(queue.process_operations())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await queue.command(result.job_id, "cancel", **observed(db, result.job_id))
    finally:
        release.set()
        await asyncio.wait_for(pending, 3)
    assert (await queue.get_task(result.task_id))["status"] == "CANCELLED"
    assert not await queue._process_one()
    assert calls == [1]


@pytest.mark.asyncio
async def test_authenticated_torrent_failure_rotates_before_remote_push(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    with db.connection() as connection:
        connection.execute("UPDATE candidates SET torrent_count = 1, torrent_hash = ? WHERE id = 1", (TORRENT_DIGEST,))
    calls = []

    async def handler(request):
        if request.url.path == "/gallerytorrents.php":
            calls.append(member(request))
            if len(calls) == 1:
                return httpx.Response(401, text="Login required")
            return httpx.Response(200, text=TORRENT_PAGE.format(digest=TORRENT_DIGEST))
        assert request.url.host == "ehtracker.org" and not request.headers["Cookie"]
        return httpx.Response(200, content=TORRENT_PAYLOAD)

    async def config():
        return TorrentClientConfig("http://qbittorrent", "fixture", "fixture")
    fake = FakeClient()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client, httpx.AsyncClient(transport=fake.transport()) as qbit:
        torrent = TorrentService(db, tmp_path, config_provider=config, credentials_provider=lambda: _exhentai_credentials(service),
            http_client=client, client_http_client=qbit, account_scope=service.use_kind)
        queue = DownloadService(db, tmp_path, account_scope=service.use_kind, torrent_push=torrent.push_for_candidate,
            exhentai_source=DownloadAccountPool(service).select)
        result = await queue.enqueue_torrent_download(1)
        await queue._process_one()
        assert not fake.added
        await queue.process_operations()
        await queue._process_one()
        task = await queue.get_task(result.task_id)
        assert task["status"] == "WAITING_TORRENT" and len(task["attempts"]) == 2
        assert len(calls) == 2 and calls[0] != calls[1]
        assert len(fake.added) == 1 and TORRENT_PAYLOAD in fake.added[0]


@pytest.mark.asyncio
async def test_account_disable_waits_for_paid_request_cleanup_then_blocks_new_charge(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    posts = []

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if request.method == "POST":
            posts.append(member(request))
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await release.wait()
        return httpx.Response(200, text=FORM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        queue = queue_for(db, service, client, tmp_path)
        service._process_queue_operations = queue.process_operations
        result = await queue.enqueue_exhentai_download(1)
        worker = asyncio.create_task(queue._process_one())
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with db.connection() as connection:
                account_id = connection.execute("SELECT account_id FROM eh_archive_intents").fetchone()[0]
            account = await service.registry.get(account_id)
            await service.stop_account(account_id, "disable", expected_revision=account["revision"], idempotency_key="disable-paid")
            stopping = asyncio.create_task(service.process_operations())
            await asyncio.wait_for(cancelled.wait(), 3)
            assert not stopping.done() and len(posts) == 1
        finally:
            release.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 3)
            await asyncio.gather(worker, return_exceptions=True)
        task = await queue.get_task(result.task_id)
        assert task["status"] == "FAILED" and len(task["attempts"]) == 1
        with db.connection() as connection:
            assert connection.execute("SELECT status FROM eh_archive_intents").fetchone() == ("UNKNOWN",)
            assert connection.execute("SELECT error_code FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("EH_ARCHIVE_RESULT_UNKNOWN",)
        assert len(posts) == 1 and not (await service.registry.get(account_id))["enabled"]


@pytest.mark.asyncio
async def test_ready_archive_download_retry_reuses_confirmed_paid_locator(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    posts, files = [], []

    async def handler(request):
        if str(request.url) == GALLERY:
            return httpx.Response(200, text=GALLERY_HTML)
        if str(request.url) == ENTRY:
            if request.method == "POST":
                posts.append(member(request))
            return httpx.Response(200, text=READY if request.method == "POST" else FORM)
        if str(request.url) == CONTINUE:
            return httpx.Response(200, text=LANDING)
        files.append(str(request.url))
        return httpx.Response(503) if len(files) == 1 else httpx.Response(200, content=ZIP)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        queue = queue_for(db, service, client, tmp_path)
        result = await queue.enqueue_exhentai_download(1)
        await queue._process_one()
        assert (await queue.get_task(result.task_id))["status"] == "FAILED"
        retry = await queue.command(result.job_id, "retry", **observed(db, result.job_id))
        queue = queue_for(db, service, client, tmp_path)
        await queue._process_one()
        assert (await queue.get_task(retry["task_id"]))["status"] == "COMPLETED"
        assert len(posts) == 1 and files == [FILE, FILE]


@pytest.mark.asyncio
async def test_torrent_shared_limit_finishes_without_rotating_or_inventing_remote_resources(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    with db.connection() as connection:
        connection.execute("UPDATE candidates SET torrent_count = 1, torrent_hash = ? WHERE id = 1", (TORRENT_DIGEST,))
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "30"})
    async def config():
        return TorrentClientConfig("http://qbittorrent", "fixture", "fixture")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        torrent = TorrentService(db, tmp_path, config_provider=config,
            credentials_provider=lambda: _exhentai_credentials(service), http_client=client, account_scope=service.use_kind)
        queue = DownloadService(db, tmp_path, account_scope=service.use_kind, torrent_push=torrent.push_for_candidate,
            exhentai_source=DownloadAccountPool(service).select)
        result = await queue.enqueue_torrent_download(1)
        await queue._process_one()
        task = await queue.get_task(result.task_id)
        assert task["status"] == "FAILED" and len(task["attempts"]) == 1
        assert task["attempts"][0]["error_code"] == "EXHENTAI_RATE_LIMITED"
        assert task["operations"] == []
        assert calls == ["/gallerytorrents.php"]
        with db.connection() as connection:
            assert connection.execute("SELECT count(*) FROM queue_remote_resources").fetchone() == (0,)


@pytest.mark.asyncio
async def test_magnet_does_not_borrow_eh_credentials_or_wait_for_eh_site_cooldown(accounts, tmp_path):
    db, service, _ = accounts
    seed(db)
    with db.connection() as connection:
        connection.execute("UPDATE candidates SET magnet_url = ?, torrent_hash = ? WHERE id = 1",
            ("magnet:?xt=urn:btih:" + TORRENT_DIGEST, TORRENT_DIGEST))
        connection.execute("INSERT INTO eh_shared_backoff(scope, cooldown_until, error_code) "
                           "VALUES('SITE', datetime('now', '+1 hour'), 'EXHENTAI_RATE_LIMITED')")
    async def config():
        return TorrentClientConfig("http://qbittorrent", "fixture", "fixture")
    async def no_credentials():
        pytest.fail("Magnet push must not read EH credentials")
    fake = FakeClient()
    async with httpx.AsyncClient(transport=fake.transport()) as qbit:
        torrent = TorrentService(db, tmp_path, config_provider=config, credentials_provider=no_credentials,
            client_http_client=qbit, account_scope=service.use_kind)
        queue = DownloadService(db, tmp_path, account_scope=service.use_kind, torrent_push=torrent.push_for_candidate,
            exhentai_source=DownloadAccountPool(service).select)
        result = await queue.enqueue_torrent_download(1)
        await queue._process_one()
        assert (await queue.get_task(result.task_id))["status"] == "WAITING_TORRENT"
        assert len(fake.added) == 1
        with db.connection() as connection:
            assert connection.execute("SELECT count(*) FROM account_execution_bindings").fetchone() == (0,)
