"""Limits are shared, credential failures are versioned, and HTML is validated."""
import httpx
import asyncio
import pytest

from app.connections.runtime import current_account_context
from app.exhentai.downloader import ExHentaiDownloadError
from app.exhentai.gdata_client import GDATA_ENDPOINT
from tests.integration.test_eh_account_rotation import accounts
from tests.integration.test_eh_metadata_accounts import gallery_service
from tests.integration.test_exhentai_torrents import make_candidate, GDATA_RESPONSE
from app.exhentai.health import ExHentaiHealth


@pytest.mark.asyncio
async def test_public_rate_limit_stops_before_html_and_survives_service_recreation(accounts, tmp_path):
    db, service, _ = accounts
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        assert str(request.url) == GDATA_ENDPOINT
        return httpx.Response(429, headers={"Retry-After": "180"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            gallery = gallery_service(db, service, tmp_path, client)
            with pytest.raises(ExHentaiDownloadError) as limited:
                await gallery.fetch_metadata_for_gallery(123, "token")
            assert limited.value.retry_after > 0
    assert requests == [GDATA_ENDPOINT]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM eh_metadata_attempts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM account_rotation").fetchone() == (0,)


@pytest.mark.asyncio
async def test_html_rate_limit_does_not_rotate_or_invalidate_other_accounts(accounts, tmp_path):
    db, service, ids = accounts
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        return httpx.Response(429, headers={"Retry-After": "90"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)
        with pytest.raises(ExHentaiDownloadError) as limited:
            await gallery.fetch_metadata_for_gallery(123, "token")
        assert limited.value.retry_after > 0
        with pytest.raises(ExHentaiDownloadError):
            await gallery.fetch_metadata_for_gallery(124, "other-token")
    assert len(requests) == 2
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM eh_metadata_attempts").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM eh_account_health").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM external_accounts WHERE enabled = 1").fetchone() == (len(ids),)


@pytest.mark.asyncio
async def test_explicit_login_failure_blocks_only_that_version_until_verified(accounts, tmp_path):
    db, service, _ = accounts
    ordered = await service.registry.list_accounts()
    invalid_id = ordered[0]["account_id"]
    requested = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        account_id = current_account_context.get().account_id
        requested.append(account_id)
        if account_id == invalid_id:
            return httpx.Response(200, text="<html><title>ExHentai</title>You must be logged in.</html>")
        return httpx.Response(200, text='<html>ExHentai<h1 id="gn">valid</h1></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)
        assert (await gallery.fetch_metadata_for_gallery(123, "token"))[0]["Title"] == "valid"
        assert (await gallery.fetch_metadata_for_gallery(124, "other-token"))[0]["Title"] == "valid"
        assert requested.count(invalid_id) == 1
        assert await service.select_account("EH", candidate_versions={invalid_id: 1}) is None
        account = await service.registry.get(invalid_id)
        await service.registry.record_identity(invalid_id, credential_revision=1, expected_revision=account["revision"],
                                              remote_identity=account["remote_identity"], platform_name=account["platform_name"])
        assert (await service.select_account("EH", candidate_versions={invalid_id: 1}))["account_id"] == invalid_id
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM eh_account_health WHERE account_id = ?", (invalid_id,)).fetchone() == (0,)


@pytest.mark.asyncio
async def test_generic_error_page_is_not_a_gallery_and_does_not_poison_credentials(accounts, tmp_path):
    db, service, ids = accounts
    requested = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        requested.append(current_account_context.get().account_id)
        return httpx.Response(200, text="<html><title>ExHentai service error</title>Try again later</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as failed:
            await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "token")
    assert failed.value.code == "EH_ALL_ACCOUNTS_FAILED"
    assert len(requested) == len(ids)
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM metadata_values").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM eh_account_health").fetchone() == (0,)


@pytest.mark.asyncio
async def test_html_ip_ban_is_shared_even_with_http_200(accounts, tmp_path):
    db, service, _ = accounts
    html_requests = []

    async def handler(request):
        if str(request.url) == GDATA_ENDPOINT:
            return httpx.Response(200, json={"gmetadata": []})
        html_requests.append(request)
        return httpx.Response(200, text="Your IP address has been temporarily banned for excessive pageloads.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as limited:
            await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(123, "token")
    assert limited.value.code == "EXHENTAI_RATE_LIMITED"
    assert len(html_requests) == 1


@pytest.mark.asyncio
async def test_public_backoff_is_not_promoted_to_site_backoff_by_background_enrichment(accounts, tmp_path):
    db, service, _ = accounts
    await make_candidate(db, 4108964, "torrenttoken")
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "90"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gallery = gallery_service(db, service, tmp_path, client)
        with pytest.raises(ExHentaiDownloadError):
            await gallery.fetch_metadata_for_gallery(123, "token")
        assert await gallery.enrich_missing_metadata((1,)) == 0
    assert len(requests) == 1
    with db.connection() as connection:
        assert connection.execute("SELECT scope FROM eh_shared_backoff").fetchall() == [("GDATA",)]
    assert await service.select_account("EH") is not None


@pytest.mark.asyncio
async def test_expired_public_backoff_allows_a_fresh_request(accounts, tmp_path):
    db, service, _ = accounts
    health = ExHentaiHealth(db)
    await health.record_failure(ExHentaiDownloadError("EXHENTAI_GDATA_RATE_LIMITED", "fixture",
                                                   failure_scope="gdata", retry_after=60))
    with db.connection() as connection:
        connection.execute("UPDATE eh_shared_backoff SET cooldown_until = datetime('now', '-1 second')")

    async def handler(request):
        assert str(request.url) == GDATA_ENDPOINT
        return httpx.Response(200, json=GDATA_RESPONSE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gallery_service(db, service, tmp_path, client).fetch_metadata_for_gallery(4108964, "torrenttoken")
    assert result[0]["Title"] == "Torrent Sample"


@pytest.mark.asyncio
async def test_archive_capability_cooldown_does_not_disable_metadata(accounts):
    db, service, ids = accounts
    health = ExHentaiHealth(db)
    await health.record_failure(ExHentaiDownloadError("EH_GP_INSUFFICIENT", "fixture",
        failure_scope="capability", retry_after=120), account_id=ids[0], credential_revision=1, capability="ARCHIVE")
    assert await service.select_account("EH", candidate_versions={ids[0]: 1}, capability="ARCHIVE") is None
    assert (await service.select_account("EH", candidate_versions={ids[0]: 1}, capability="METADATA"))["account_id"] == ids[0]
    with db.connection() as connection:
        connection.execute("UPDATE eh_account_health SET cooldown_until = datetime('now', '-1 second')")
    assert (await service.select_account("EH", candidate_versions={ids[0]: 1}, capability="ARCHIVE"))["account_id"] == ids[0]


@pytest.mark.asyncio
async def test_late_old_credential_failure_does_not_block_new_private_version(accounts):
    from tests.integration.test_account_lifecycle import no_queue_operations
    db, service, ids = accounts
    service._process_queue_operations = no_queue_operations
    old = await service.registry.get(ids[0])
    await service.stop_account(ids[0], "disable", expected_revision=old["revision"], idempotency_key="health-edit")
    assert await service.process_operations() == 1
    current = await service.registry.get(ids[0])
    current = await service.registry.edit(ids[0], expected_revision=current["revision"], fields={"igneous": "new-fixture"})
    current = await service.registry.record_identity(ids[0], credential_revision=current["credential_revision"],
        expected_revision=current["revision"], remote_identity=old["remote_identity"], platform_name=old["platform_name"])
    current = await service.enable_account(ids[0], expected_revision=current["revision"])
    await ExHentaiHealth(db).record_failure(ExHentaiDownloadError("EXHENTAI_METADATA_AUTH", "fixture", failure_scope="credential"),
                                          account_id=ids[0], credential_revision=old["credential_revision"])
    assert current["credential_revision"] != old["credential_revision"]
    assert (await service.select_account("EH", candidate_versions={ids[0]: current["credential_revision"]}))["account_id"] == ids[0]


@pytest.mark.asyncio
async def test_one_invalid_cookie_does_not_hide_another_healthy_account_in_snapshot(accounts):
    db, service, ids = accounts
    service._kinds.update({account_id: "EH" for account_id in ids})
    health = ExHentaiHealth(db)
    invalid = ExHentaiDownloadError("EXHENTAI_METADATA_AUTH", "fixture", failure_scope="credential")
    await health.record_failure(invalid, account_id=ids[-1], credential_revision=1)
    assert service.account_status(ids[-1])["state"] == "error"
    assert service.snapshot().exhentai.state == "connected"
    for account_id in ids[:-1]:
        await health.record_failure(invalid, account_id=account_id, credential_revision=1)
    assert service.snapshot().exhentai.state == "error"


@pytest.mark.asyncio
async def test_verification_shares_the_account_lock_with_metadata_operations(accounts, monkeypatch):
    from app.connections.exhentai import ExHentaiApi
    db, service, ids = accounts
    entered, release, verified = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def verify(self):
        verified.set()
        return (await service.registry.get(ids[0]))["platform_name"]

    async def reading():
        async with service.use_kind("EH", account_id=ids[0]):
            entered.set()
            await release.wait()

    monkeypatch.setattr(ExHentaiApi, "verify", verify)
    reader = asyncio.create_task(reading())
    checking = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        account = await service.registry.get(ids[0])
        checking = asyncio.create_task(service.test_account(ids[0], expected_revision=account["revision"]))
        async with service.use_kind("EH", account_id=ids[1]):
            assert not verified.is_set()
    finally:
        release.set()
        await reader
        if checking is not None:
            await asyncio.wait_for(checking, 3)
    assert verified.is_set()


@pytest.mark.asyncio
async def test_verification_rate_limit_retains_saved_activation_and_backs_off_other_accounts(accounts):
    from app.connections.models import ProviderConnectionError
    db, service, ids = accounts
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service._exhentai_client = client
        for account_id in ids[:2]:
            account = await service.registry.get(account_id)
            with pytest.raises(ProviderConnectionError) as limited:
                await service.test_account(account_id, expected_revision=account["revision"])
            assert limited.value.code == "EXHENTAI_RATE_LIMITED" and limited.value.retry_after > 0
            assert (await service.registry.get(account_id))["enabled"]
            assert service._states[account_id].state == "connected"
    assert len(requests) == 1
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM eh_account_health").fetchone() == (0,)


@pytest.mark.asyncio
async def test_actual_cookie_reverification_clears_only_auth_health(accounts):
    from app.connections.models import ProviderConnectionError
    db, service, ids = accounts
    responses = ["<html><title>ExHentai</title>You must be logged in.</html>", "<html>ExHentai</html>"]

    async def handler(request):
        return httpx.Response(200, text=responses.pop(0))

    account = await service.registry.get(ids[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service._exhentai_client = client
        with pytest.raises(ProviderConnectionError) as failed:
            await service.test_account(ids[0], expected_revision=account["revision"])
        assert failed.value.code == "EXHENTAI_UNAUTHORIZED"
        assert service.account_status(ids[0])["state"] == "error"
        with db.connection() as connection:
            assert connection.execute("SELECT capability FROM eh_account_health WHERE account_id = ?", (ids[0],)).fetchall() == [("AUTH",)]
        current = await service.test_account(ids[0], expected_revision=account["revision"])
        assert current["enabled"] and service.account_status(ids[0])["state"] == "connected"
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM eh_account_health WHERE account_id = ?", (ids[0],)).fetchone() == (0,)


@pytest.mark.asyncio
async def test_archive_preflight_obeys_site_backoff_before_borrowing_or_requesting(accounts, tmp_path):
    db, service, _ = accounts
    await make_candidate(db, 4108964, "torrenttoken")
    await ExHentaiHealth(db).record_failure(ExHentaiDownloadError("EXHENTAI_RATE_LIMITED", "fixture",
                                                             failure_scope="site", retry_after=120))

    async def handler(request):
        pytest.fail("preflight must not request while site backoff is active")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExHentaiDownloadError) as limited:
            await gallery_service(db, service, tmp_path, client).preflight_archive_for_candidate(1)
    assert limited.value.code == "EXHENTAI_RATE_LIMITED" and limited.value.retry_after > 0
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM account_rotation").fetchone() == (0,)
