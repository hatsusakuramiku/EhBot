"""Archive-download coverage: the endpoint can answer with a web page.

ExHentai serves its archive through a H@H node, and every failure mode on the
way there (stale cookie, rate limit, node refusing the client) comes back as an
HTML page with HTTP 200. The downloader has to notice that at the door: a page
stored as `gallery-<gid>.zip` only fails later, inside the packer, where the
message is about ZIP or 7-Zip and says nothing about the cookie.
"""

import httpx
import pytest

from app.connections.exhentai import ExHentaiCredentials
from app.exhentai.downloader import ExHentaiDownloadError, ExHentaiDownloader


def _credentials() -> ExHentaiCredentials:
    return ExHentaiCredentials(
        ipb_member_id="10001",
        ipb_pass_hash="pass-secret",
        igneous="igneous-secret",
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_download_archive_refuses_a_login_page(tmp_path) -> None:
    page = (
        "<!DOCTYPE html>\n<html><head><title>parody: original - ExHentai.org"
        "</title></head><body>You must be logged in.</body></html>"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    destination = tmp_path / "exhentai" / "gallery-3893499.zip"
    async with _client(handler) as client:
        with pytest.raises(ExHentaiDownloadError) as raised:
            await ExHentaiDownloader(client).download_archive(
                _credentials(), "https://hath.example/archive/abc", destination
            )

    assert raised.value.code == "EXHENTAI_ARCHIVE_NOT_ZIP"
    assert "Cookie" in raised.value.public_message
    # Nothing may be left behind: the packer would pick the file up and fail
    # with an archive error that hides the real cause.
    assert not destination.exists()


@pytest.mark.asyncio
async def test_download_archive_keeps_a_real_zip(tmp_path) -> None:
    payload = b"PK\x03\x04" + b"\x00" * 256

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    destination = tmp_path / "exhentai" / "gallery-3893499.zip"
    async with _client(handler) as client:
        size = await ExHentaiDownloader(client).download_archive(
            _credentials(), "https://hath.example/archive/abc", destination
        )

    assert size == len(payload)
    assert destination.read_bytes() == payload


#: The page an ExHentai gallery renders for its tags, plus the archive button.
#: `parody:original` is on nearly every doujinshi, so its link -- label
#: "original", href pointing at a tag search -- is always there to be mistaken
#: for the archive button.
_TAGGED_GALLERY_PAGE = """
<html><body>
<div id="taglist">
  <a href="https://exhentai.org/?f_search=parody%3Aoriginal">original</a>
  <a href="https://exhentai.org/?f_search=language%3Achinese">chinese</a>
</div>
<div id="gd2">
  <a href="https://exhentai.org/archiver.php?gid=3893499&amp;token=4f732d0bde&amp;or=1">Archive Download</a>
</div>
</body></html>
"""

_GALLERY_URL = "https://exhentai.org/g/3893499/4f732d0bde/"


def _posting(html: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == _GALLERY_URL
        assert request.content == b"dl=yes&p_p=0"
        return httpx.Response(200, text=html)

    return handler


@pytest.mark.asyncio
async def test_request_archive_url_prefers_the_archiver_over_a_tag_link() -> None:
    """A tag whose value is "original" must not be read as the archive button.

    It was: the old pattern matched any anchor whose text started with
    Download/Archive/Original, so a gallery page produced a tag-search URL and
    the downloader stored a search-results page as `gallery-<gid>.zip`.
    """
    async with _client(_posting(_TAGGED_GALLERY_PAGE)) as client:
        link = await ExHentaiDownloader(client).request_archive_url(
            _credentials(), 3893499, "4f732d0bde"
        )

    assert link == (
        "https://exhentai.org/archiver.php?gid=3893499&token=4f732d0bde&or=1"
    )
    assert "f_search=" not in link


@pytest.mark.asyncio
async def test_request_archive_url_refuses_a_page_with_no_archive_button() -> None:
    """No archiver link means no archive -- not "download the tag search".

    This is the intermittent failure: ExHentai answers `dl=yes` with a page that
    has no archive link (archive not generated yet, or the layout changed), and
    the only Download/Archive/Original anchors on it belong to the tag list.
    """
    page = _TAGGED_GALLERY_PAGE.replace(
        '<a href="https://exhentai.org/archiver.php?gid=3893499&amp;token=4f732d0bde&amp;or=1">Archive Download</a>',
        "",
    )
    async with _client(_posting(page)) as client:
        with pytest.raises(ExHentaiDownloadError) as raised:
            await ExHentaiDownloader(client).request_archive_url(
                _credentials(), 3893499, "4f732d0bde"
            )

    assert raised.value.code == "EXHENTAI_ARCHIVE_LINK"
    assert "f_search" not in raised.value.public_message
