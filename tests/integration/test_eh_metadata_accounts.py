"""Public metadata must not borrow an account; HTML retains its exact lease."""
import asyncio

import httpx
import pytest

from app.connections.runtime import current_account_context
from app.exhentai.gdata_client import GDATA_ENDPOINT
from app.exhentai.service import ExHentaiService
from app.exhentai.accounts import MetadataAccountPool
from app.exhentai.downloader import ExHentaiDownloadError
from app.wiring import _exhentai_credentials
from tests.integration.test_eh_account_rotation import accounts
from tests.integration.test_exhentai_torrents import GDATA_RESPONSE, make_candidate


def gallery_service(db, accounts_service, tmp_path, client):
    return ExHentaiService(
        db, tmp_path / "work", tmp_path / "library",
        lambda: _exhentai_credentials(accounts_service), http_client=client,
        account_scope=accounts_service.use_kind,
        account_pool=MetadataAccountPool(accounts_service),
    )


def assert_unused_pool(db, service):
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM account_rotation").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM account_rotation_state").fetchone() == (0,)
    assert not service.runtime._leases


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["gallery", "candidate", "enrichment"])
async def test_public_metadata_does_not_borrow_or_rotate_accounts(accounts, tmp_path, entrypoint):
    db, service, _ = accounts
    await make_candidate(db, 4108964, "torrenttoken")
    requests = []

    async def handler(request):
        assert str(request.url) == GDATA_ENDPOINT
        assert current_account_context.get() is None
        assert not service.runtime._leases
        requests.append(request)
        return httpx.Response(200, json=GDATA_RESPONSE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)
        if entrypoint == "gallery":
            metadata, _ = await gallery.fetch_metadata_for_gallery(4108964, "torrenttoken")
            assert metadata["Title"] == "Torrent Sample"
        elif entrypoint == "candidate":
            assert (await gallery.fetch_metadata_for_candidate(1))["Title"] == "Torrent Sample"
        else:
            assert await gallery.enrich_missing_metadata((1,)) == 1
    assert len(requests) == 1
    assert_unused_pool(db, service)


@pytest.mark.asyncio
async def test_empty_enrichment_does_not_consume_an_account_turn(accounts, tmp_path):
    db, service, _ = accounts
    async with httpx.AsyncClient() as client:
        assert await gallery_service(db, service, tmp_path, client)._enrich_refs([]) == 0
    assert_unused_pool(db, service)


@pytest.mark.asyncio
async def test_batch_missing_gallery_uses_html_without_a_second_public_request(accounts, tmp_path):
    db, service, _ = accounts
    await make_candidate(db, 4108964, "torrenttoken")
    requests = []
    selected = []

    async def handler(request):
        requests.append(str(request.url))
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        context = current_account_context.get()
        selected.append(context.account_id)
        assert context.values["ipb_member_id"] in request.headers["Cookie"]
        return httpx.Response(200, text='<html>ExHentai<h1 id="gn">HTML title</h1></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await gallery_service(db, service, tmp_path, client).enrich_missing_metadata((1,)) == 1
    assert requests == [GDATA_ENDPOINT, "https://exhentai.org/g/4108964/torrenttoken/"]
    assert len(selected) == 1
    with db.connection() as connection:
        assert connection.execute("SELECT field_value FROM metadata_values WHERE candidate_id = 1 AND field_name = 'Title'").fetchone() == ("HTML title",)
        assert connection.execute("SELECT next_sequence FROM account_rotation_state WHERE kind = 'EH'").fetchone() == (2,)
    assert not service.runtime._leases


@pytest.mark.asyncio
async def test_authenticated_metadata_keeps_lease_until_outer_publication_exits(accounts, tmp_path):
    db, service, _ = accounts
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    selected = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        selected.append(current_account_context.get().account_id)
        return httpx.Response(200, text='<html>ExHentai<h1 id="gn">HTML title</h1></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)

        async def publish():
            async with gallery.account_request():
                metadata, _ = await gallery.fetch_metadata_for_gallery(4108964, "torrenttoken")
                assert metadata["Title"] == "HTML title"
                assert current_account_context.get().account_id == selected[0]
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()

        publishing = asyncio.create_task(publish())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stopping = asyncio.create_task(service.runtime.stop(selected[0], 1))
            await asyncio.wait_for(cleanup.wait(), 3)
            assert not stopping.done()
        finally:
            release.set()
            if not publishing.cancelling():
                publishing.cancel()
            await asyncio.gather(publishing, return_exceptions=True)
        assert await stopping
    assert not service.runtime._leases
    assert current_account_context.get() is None


@pytest.mark.asyncio
async def test_public_gdata_does_not_send_shared_client_cookies(accounts, tmp_path):
    db, service, _ = accounts

    async def handler(request):
        assert not request.headers.get("Cookie")
        return httpx.Response(200, json=GDATA_RESPONSE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("unrelated_fixture", "must-not-be-sent", domain=".e-hentai.org")
        gallery = gallery_service(db, service, tmp_path, client)
        await gallery.fetch_metadata_for_gallery(4108964, "torrenttoken")


@pytest.mark.asyncio
async def test_metadata_failure_exits_a_before_b_and_retains_b_through_publication(accounts, tmp_path):
    db, service, _ = accounts
    seen = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        context = current_account_context.get()
        assert set(service.runtime._leases) == {(context.account_id, context.credential_revision)}
        seen.append(context.account_id)
        assert f'ipb_member_id={context.values["ipb_member_id"]}' in request.headers["Cookie"]
        return httpx.Response(503) if len(seen) == 1 else httpx.Response(
            200, text='<html>ExHentai<h1 id="gn">B result</h1></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)
        async with gallery.account_request():
            result, _ = await gallery.fetch_metadata_for_gallery(123, "token")
            assert result["Title"] == "B result"
            assert current_account_context.get().account_id == seen[-1]
    assert len(seen) == len(set(seen)) == 2
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM eh_metadata_operations").fetchall() == [("SUCCEEDED",)]
        assert connection.execute("SELECT account_id, status, error_code FROM eh_metadata_attempts ORDER BY ordinal").fetchall() == [
            (seen[0], "FAILED", "EXHENTAI_METADATA_HTTP"), (seen[1], "SUCCEEDED", None)]
    assert not service.runtime._leases


@pytest.mark.asyncio
async def test_metadata_exhaustion_is_finite_and_does_not_add_later_accounts(accounts, tmp_path):
    import json
    from app.connections.models import ProviderStatus
    db, service, ids = accounts
    seen = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        seen.append(current_account_context.get().account_id)
        if len(seen) == 1:
            row = await service.registry.create("EH", "late", {"ipb_member_id": "late", "ipb_pass_hash": "fixture", "igneous": "fixture"})
            row = await service.registry.record_identity(row["account_id"], expected_revision=row["revision"],
                credential_revision=row["credential_revision"], remote_identity="late", platform_name="late")
            row = await service.registry.enable(row["account_id"], expected_revision=row["revision"])
            service._states[row["account_id"]] = ProviderStatus("connected", True)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as failed:
            await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "private-link-token")
    assert failed.value.code == "EH_ALL_ACCOUNTS_FAILED"
    assert len(seen) == len(ids) and set(seen) == set(ids)
    assert {item["status"] for item in failed.value.account_results} == {"FAILED"}
    with db.connection() as connection:
        row = connection.execute("SELECT candidates_json, results_json, status, error_code FROM eh_metadata_operations").fetchone()
        assert len(json.loads(row[0])) == len(json.loads(row[1])) == 3
        assert row[2:] == ("FAILED", "EH_ALL_ACCOUNTS_FAILED")
        assert "private-link-token" not in str(row)
        assert connection.execute("SELECT count(*) FROM eh_metadata_attempts").fetchone() == (3,)


@pytest.mark.asyncio
async def test_metadata_unavailable_accounts_are_not_reported_as_supplier_failures(accounts, tmp_path):
    db, service, _ = accounts
    with db.connection() as connection:
        connection.execute("UPDATE external_accounts SET enabled = 0 WHERE kind = 'EH'")

    async def handler(request):
        assert str(request.url) == GDATA_ENDPOINT
        return httpx.Response(200, json={"gmetadata": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as failed:
            await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "token")
    assert failed.value.code == "EH_NO_ACCOUNT_AVAILABLE"
    assert {item["error_code"] for item in failed.value.account_results} == {"ACCOUNT_DISABLED"}
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM eh_metadata_attempts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_metadata_cancel_waits_for_cleanup_and_does_not_try_another_account(accounts, tmp_path):
    db, service, _ = accounts
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        seen.append(current_account_context.get().account_id)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reading = asyncio.create_task(gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "token"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            reading.cancel()
            await asyncio.wait_for(cleanup.wait(), 3)
            assert not reading.done()
            assert len(service.runtime._leases) == 1
        finally:
            release.set()
            if not reading.cancelling():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
    with db.connection() as connection:
        assert connection.execute("SELECT status, error_code FROM eh_metadata_operations").fetchall() == [("CANCELLED", "EH_METADATA_CANCELLED")]
        assert connection.execute("SELECT status FROM eh_metadata_attempts").fetchall() == [("CANCELLED",)]
    assert len(seen) == 1 and not service.runtime._leases


@pytest.mark.asyncio
async def test_cancel_during_operation_registration_does_not_leave_running_record(accounts, tmp_path, monkeypatch):
    from threading import Event
    db, service, _ = accounts
    registered, release = Event(), Event()

    async def handler(request):
        assert str(request.url) == GDATA_ENDPOINT
        return httpx.Response(200, json={"gmetadata": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)
        begin = gallery._account_pool._begin

        def blocked(*args):
            result = begin(*args)
            registered.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(gallery._account_pool, "_begin", blocked)
        reading = asyncio.create_task(gallery.fetch_metadata_for_gallery(123, "token"))
        try:
            assert await asyncio.to_thread(registered.wait, 3)
            reading.cancel()
            await asyncio.sleep(0)
            assert not reading.done()
        finally:
            release.set()
            if not reading.cancelling():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
    with db.connection() as connection:
        assert connection.execute("SELECT status, error_code FROM eh_metadata_operations").fetchall() == [("CANCELLED", "EH_METADATA_CANCELLED")]
        assert connection.execute("SELECT count(*) FROM eh_metadata_attempts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_account_stopped_after_selection_is_skipped_before_supplier_request(accounts, tmp_path, monkeypatch):
    db, service, _ = accounts
    select = service.select_account
    skipped, used = [], []

    async def select_then_stop(*args, **kwargs):
        account = await select(*args, **kwargs)
        if not skipped:
            skipped.append(account["account_id"])
            await service.stop_account(account["account_id"], "disable", expected_revision=account["revision"],
                                       idempotency_key="metadata-stop-before-use")
        return account

    monkeypatch.setattr(service, "select_account", select_then_stop)

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        used.append(current_account_context.get().account_id)
        return httpx.Response(200, text='<html>ExHentai<h1 id="gn">B result</h1></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "token"))[0]["Title"] == "B result"
    assert len(used) == 1 and used[0] != skipped[0]
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM eh_metadata_attempts ORDER BY ordinal").fetchall() == [("UNAVAILABLE",), ("SUCCEEDED",)]


@pytest.mark.asyncio
async def test_concurrent_rotation_does_not_reorder_captured_metadata_fallbacks(accounts, tmp_path):
    db, service, _ = accounts
    ordered = [row["account_id"] for row in await service.registry.list_accounts()]
    seen = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        seen.append(current_account_context.get().account_id)
        if len(seen) == 1:
            # Another operation consumes B's global turn while A is in flight.
            await service.select_account("EH", candidate_versions={ordered[1]: 1})
            return httpx.Response(503)
        return httpx.Response(200, text='<html>ExHentai<h1 id="gn">B result</h1></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "token")
    assert seen == ordered[:2]
