"""A target probe cannot revoke the old execution or bypass command receipts."""
import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.connections.exhentai import ExHentaiCredentials
from app.downloads.service import DownloadError, DownloadService
from app.exhentai.service import ExHentaiService
from app.main import create_app
from app.telegraph.service import TelegraphService
from tests.integration.test_api_auth import complete_first_run, make_settings
from tests.integration.test_queue_control import database, observed, pending
from tests.integration.test_realtime import call, hello


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["EXHENTAI", "TELEGRAPH"])
async def test_unconfigured_target_does_not_change_running_attempt(database, tmp_path, provider):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    draft = observed(database, job_id)
    before = await service.get_task(draft["task_id"])
    with pytest.raises(DownloadError) as error:
        await service.command(job_id, "switch-source", provider=provider, **draft)
    assert error.value.code == f"{provider}_NOT_CONFIG"
    assert await service.get_task(draft["task_id"]) == before
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM queue_command_receipts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_refused_preview_probe_leaves_actual_producer_running(database, tmp_path):
    entered, release, cancelled = (asyncio.Event() for _ in range(3))
    async def producer(candidate_id):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    def unreachable(request):
        raise httpx.ConnectError("controlled outage", request=request)
    with database.connection() as c:
        c.execute("UPDATE candidates SET preview_url = 'https://telegra.ph/Book-10-14'")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        target = TelegraphService(database, tmp_path, http_client=client)
        service = DownloadService(database, tmp_path, exhentai_download=producer,
                                  telegraph_preflight=target.preflight_for_candidate)
        job_id = pending(database, "EXHENTAI")
        worker = asyncio.create_task(service._process_one())
        await asyncio.wait_for(entered.wait(), 2)
        draft = observed(database, job_id)
        before = await service.get_task(draft["task_id"])
        try:
            with pytest.raises(DownloadError) as error:
                await service.command(job_id, "switch-source", provider="TELEGRAPH", **draft)
            assert error.value.code == "TELEGRAPH_PAGE_UNREACHABLE"
            assert not worker.done() and not cancelled.is_set()
            assert await service.get_task(draft["task_id"]) == before
        finally:
            release.set()
            await asyncio.wait_for(worker, 2)


@pytest.mark.asyncio
async def test_preview_preflight_validates_pages_without_downloading_images(database, tmp_path):
    requests = []
    def page(request):
        requests.append(request.url)
        assert request.url.host == "api.telegra.ph"
        return httpx.Response(200, json={"ok": True, "result": {"title": "Book", "content": [
            {"tag": "img", "attrs": {"src": "https://images.example/1.jpg"}},
        ]}})
    with database.connection() as c:
        c.execute("UPDATE candidates SET preview_url = 'https://telegra.ph/Book-10-14'")
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source) "
                  "VALUES(1, 'Pages', '2', 'MANUAL')")
    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as client:
        target = TelegraphService(database, tmp_path, http_client=client)
        service = DownloadService(database, tmp_path, telegraph_preflight=target.preflight_for_candidate)
        job_id = pending(database)
        draft = observed(database, job_id)
        with pytest.raises(DownloadError) as error:
            await service.command(job_id, "switch-source", provider="TELEGRAPH", **draft)
        assert error.value.code == "TELEGRAPH_PAGE_COUNT_MISMATCH"
        with database.connection() as c:
            c.execute("UPDATE metadata_values SET field_value = '1' WHERE field_name = 'Pages'")
        accepted = await service.command(job_id, "switch-source", provider="TELEGRAPH", **draft)
        assert accepted["state"] == "STOPPING"
        assert len(requests) == 2
        with database.connection() as c:
            assert c.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["reference", "token", "credentials", "unauthorized"])
async def test_eh_preflight_does_not_request_paid_archive(database, tmp_path, missing):
    requests = []
    async def credentials():
        return None if missing == "credentials" else ExHentaiCredentials("test-member", "test-hash", "test-igneous")
    def rejected(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/g/1234/test-token/"
        assert not request.url.query
        return httpx.Response(200, text="login required")
    with database.connection() as c:
        if missing != "reference":
            c.execute("UPDATE candidates SET ex_gid = 1234, ex_gallery_token = ?",
                      (None if missing == "token" else "test-token",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        target = ExHentaiService(database, tmp_path, tmp_path / "library", credentials, http_client=client)
        service = DownloadService(database, tmp_path, exhentai_preflight=target.preflight_archive_for_candidate)
        job_id = pending(database)
        service._claim_pending_job_sync()
        draft = observed(database, job_id)
        before = await service.get_task(draft["task_id"])
        with pytest.raises(DownloadError) as error:
            await service.command(job_id, "switch-source", provider="EXHENTAI", **draft)
        expected = {"reference": "CANDIDATE_HAS_NO_EX_REFERENCE", "token": "CANDIDATE_HAS_NO_EX_REFERENCE",
                    "credentials": "EXHENTAI_NOT_CONFIG", "unauthorized": "EXHENTAI_METADATA_AUTH"}
        assert error.value.code == expected[missing]
        assert await service.get_task(draft["task_id"]) == before
        assert len(requests) == (1 if missing == "unauthorized" else 0)


@pytest.mark.asyncio
async def test_preflight_is_outside_writer_lock_and_final_cas_rejects_stale_probe(database, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(candidate_id):
        entered.set()
        await release.wait()
    service = DownloadService(database, tmp_path, telegraph_preflight=probe)
    job_id = pending(database)
    draft = observed(database, job_id)
    switch = asyncio.create_task(service.command(job_id, "switch-source", provider="TELEGRAPH", **draft))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        cancelled = await asyncio.wait_for(service.command(job_id, "cancel", **observed(database, job_id)), 2)
        assert cancelled["state"] == "CANCELLED"
    finally:
        release.set()
    with pytest.raises(DownloadError) as error:
        await asyncio.wait_for(switch, 2)
    assert error.value.code == "REVISION_CONFLICT"
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM queue_operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_receipt_replay_skips_target_probe_after_configuration_change(database, tmp_path):
    probes = []
    async def probe(candidate_id):
        probes.append(candidate_id)
    service = DownloadService(database, tmp_path, telegraph_preflight=probe)
    job_id = pending(database)
    draft = observed(database, job_id)
    accepted = await service.command(job_id, "switch-source", provider="TELEGRAPH", **draft)
    service._telegraph_preflight = None
    assert await service.command(job_id, "switch-source", provider="TELEGRAPH", **draft) == accepted
    assert probes == [1]
    with pytest.raises(DownloadError) as error:
        await service.command(job_id, "switch-source", provider="EXHENTAI", **draft)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_concurrent_replay_returns_receipt_even_when_its_probe_fails(database, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    async def probe(candidate_id):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
            raise DownloadError("TELEGRAPH_PAGE_UNREACHABLE", "controlled outage")
    service = DownloadService(database, tmp_path, telegraph_preflight=probe)
    job_id = pending(database)
    draft = observed(database, job_id)
    first = asyncio.create_task(service.command(job_id, "switch-source", provider="TELEGRAPH", **draft))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        accepted = await service.command(job_id, "switch-source", provider="TELEGRAPH", **draft)
    finally:
        release.set()
    assert await asyncio.wait_for(first, 2) == accepted
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM queue_operations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_internal_switch_uses_same_target_preflight(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    draft = observed(database, job_id)
    before = await service.get_task(draft["task_id"])
    with pytest.raises(DownloadError) as error:
        await service.switch_source(job_id, "EXHENTAI")
    assert error.value.code == "EXHENTAI_NOT_CONFIG"
    assert await service.get_task(draft["task_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["TELEGRAPH", "EXHENTAI"])
async def test_changed_target_reference_during_probe_does_not_revoke_old_task(database, tmp_path, provider):
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(candidate_id):
        entered.set()
        await release.wait()
    service = DownloadService(database, tmp_path, telegraph_preflight=probe, exhentai_preflight=probe)
    job_id = pending(database)
    service._claim_pending_job_sync()
    draft = observed(database, job_id)
    before = await service.get_task(draft["task_id"])
    switch = asyncio.create_task(service.command(job_id, "switch-source", provider=provider, **draft))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with database.connection() as c:
            c.execute("UPDATE candidates SET ex_gid = 1234, ex_gallery_token = 'changed', "
                      "preview_url = 'https://telegra.ph/Changed-10-14'")
    finally:
        release.set()
    with pytest.raises(DownloadError) as error:
        await asyncio.wait_for(switch, 2)
    assert error.value.code == "SOURCE_CHANGED"
    assert await service.get_task(draft["task_id"]) == before


def test_sync_internal_switch_cannot_bypass_preflight(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    with pytest.raises(DownloadError) as error:
        service._queue_control.internal_command(job_id, "switch-source", provider="TELEGRAPH")
    assert error.value.code == "SOURCE_PREFLIGHT_REQUIRED"


def test_http_web_ws_share_missing_target_refusal(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app) as client:
        complete_first_run(client, settings)
        client.portal.call(app.state.download_service.stop)
        db = app.state.database
        with db.connection() as c:
            c.execute("INSERT INTO candidates(id, status, filter_result) VALUES(1, 'APPROVED', 'ACCEPT')")
        job_id = pending(db)
        draft = {**observed(db, job_id), "provider": "TELEGRAPH"}
        csrf = client.get("/candidates").context["csrf_token"]
        before = client.portal.call(app.state.download_service.get_task, draft["task_id"])
        response = client.post(f"/api/v1/jobs/{job_id}/switch-source", json=draft, headers={CSRF_HEADER: csrf})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TELEGRAPH_PAGE_UNREACHABLE"
        form = client.post(f"/activity/jobs/{job_id}/switch-source", data={**draft, "csrf_token": csrf},
                           follow_redirects=False)
        assert form.status_code == 303 and "error=" in form.headers["location"]
        catalogue = client.get("/api/v1/realtime").json()["commands"]
        name = next(k for k, v in catalogue.items() if v["path"] == "/api/v1/jobs/{job_id}/switch-source")
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            result, _ = call(ws, name, {"params": {"job_id": str(job_id)}, "body": draft})
            assert result["type"] == "command.error"
            assert result["error"]["code"] == "TELEGRAPH_PAGE_UNREACHABLE"
        assert client.portal.call(app.state.download_service.get_task, draft["task_id"]) == before
        async def changed_probe(candidate_id):
            with db.connection() as c:
                c.execute("UPDATE candidates SET preview_url = 'https://telegra.ph/Changed-10-14' WHERE id = ?",
                          (candidate_id,))
        app.state.download_service._telegraph_preflight = changed_probe
        changed = client.post(f"/api/v1/jobs/{job_id}/switch-source", json=draft, headers={CSRF_HEADER: csrf})
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "SOURCE_CHANGED"
        assert client.portal.call(app.state.download_service.get_task, draft["task_id"]) == before
