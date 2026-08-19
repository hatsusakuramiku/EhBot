import httpx
import pytest

from app.connections.exhentai import ExHentaiApi, ExHentaiCredentials


@pytest.mark.asyncio
async def test_exhentai_api_verifies_cookie_session() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://exhentai.org/"
        cookie = request.headers["cookie"]
        assert "ipb_member_id=10001" in cookie
        assert "ipb_pass_hash=pass-secret" in cookie
        assert "igneous=igneous-secret" in cookie
        return httpx.Response(200, text="<html><title>ExHentai.org</title></html>")

    credentials = ExHentaiCredentials(
        ipb_member_id="10001",
        ipb_pass_hash="pass-secret",
        igneous="igneous-secret",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        identity = await ExHentaiApi(credentials, client).verify()

    assert identity == "Member 10001"
