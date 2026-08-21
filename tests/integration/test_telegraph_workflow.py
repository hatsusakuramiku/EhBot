"""End-to-end coverage for the Telegraph preview-page download route."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
import zipfile

import httpx
from fastapi.testclient import TestClient
import pytest

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.conversion.service import ConversionService
from app.db.database import Database
from app.downloads.models import PROVIDER_TELEGRAPH
from app.downloads.service import DownloadService
from app.main import create_app
from app.telegraph.service import TelegraphService


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 512

PAGE_URL = "https://telegra.ph/Sample-Book-01-01"


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
    )


def public_resolver(host: str):
    return (ipaddress.ip_address("93.184.216.34"),)


def build_transport(page_count: int, *, served: int | None = None):
    """A fake Telegraph API plus image host.

    `served` lets a test publish a page that claims more images than the host
    will hand over, which is how a split preview page behaves in the wild.
    """
    images = [
        {"tag": "img", "attrs": {"src": f"https://pic.example/{index}"}}
        for index in range(1, (served or page_count) + 1)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegra.ph":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "title": "Sample Book",
                        "author_name": "channel",
                        "content": [{"tag": "figure", "children": images}],
                    },
                },
            )
        return httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )

    return httpx.MockTransport(handler)


async def seed_preview_candidate(
    database: Database,
    *,
    attachment_bytes: int = 0,
    with_gallery: bool = True,
    pages: int | None = None,
) -> int:
    """Create a candidate shaped like the real 「预览 + 原始地址」 message.

    The preview URL only reaches the database through a `text_link` entity,
    because that is exactly how the sampled channel posts it.
    """
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100999,
        display_name="Preview Channel",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    caption = "Sample Book\n预览\n"
    if with_gallery:
        caption += "原始地址: https://exhentai.org/g/4108964/previewtoken/"
    post: dict = {
        "message_id": 500,
        "date": 1_700_030_000,
        "chat": {"id": -100999, "title": "Preview Channel"},
        "caption": caption,
        "caption_entities": [
            {
                "type": "text_link",
                "offset": caption.index("预览"),
                "length": 2,
                "url": PAGE_URL,
            }
        ],
    }
    if attachment_bytes:
        post["document"] = {
            "file_id": "archive-500",
            "file_unique_id": "archive-500-uniq",
            "file_name": "book.zip",
            "mime_type": "application/zip",
            "file_size": attachment_bytes,
        }
    await database.save_telegram_updates(
        [{"update_id": 500, "channel_post": post}]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    assert candidates, "expected the preview message to become a candidate"
    candidate_id = candidates[0].candidate_id
    if pages is not None:
        with database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source) "
                "VALUES (?, 'Pages', ?, 'EXHENTAI')",
                (candidate_id, str(pages)),
            )
    return candidate_id


def build_service(
    database: Database,
    tmp_path: Path,
    *,
    page_count: int,
    served: int | None = None,
    require_match: bool = True,
) -> TelegraphService:
    return TelegraphService(
        database,
        tmp_path / "work",
        http_client=httpx.AsyncClient(
            transport=build_transport(page_count, served=served)
        ),
        require_filecount_match=require_match,
        resolver=public_resolver,
    )


def authenticate(client: TestClient, settings: Settings) -> None:
    bootstrap_password = (
        settings.data_path / "bootstrap_admin_password"
    ).read_text(encoding="utf-8")
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/change-password")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


def test_a_preview_link_in_an_entity_reaches_the_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = asyncio.run(seed_preview_candidate(database))

    candidate = asyncio.run(database.get_candidate(candidate_id))

    assert candidate is not None
    assert candidate.preview_url == PAGE_URL


@pytest.mark.asyncio
async def test_the_preview_page_becomes_an_archive_artifact(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=15)
    service = build_service(database, tmp_path, page_count=15)

    result = await service.download_for_candidate(candidate_id)

    assert result.image_count == 15
    with zipfile.ZipFile(result.archive_path) as archive:
        assert len(archive.namelist()) == 15
        assert archive.namelist()[0] == "0001.jpg"
    jobs = await DownloadService(
        database, tmp_path / "work"
    ).list_jobs_for_candidate(candidate_id)
    assert [job.provider for job in jobs] == [PROVIDER_TELEGRAPH]
    assert jobs[0].state == "COMPLETED"
    assert jobs[0].artifact_path == result.archive_path


@pytest.mark.asyncio
async def test_provenance_is_recorded_in_metadata_and_job_details(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=3)
    service = build_service(database, tmp_path, page_count=3)

    await service.download_for_candidate(candidate_id)

    with database._connect() as connection:  # noqa: SLF001
        scan = connection.execute(
            "SELECT field_value, value_source FROM metadata_values "
            "WHERE candidate_id = ? AND field_name = 'ScanInformation'",
            (candidate_id,),
        ).fetchone()
        details = connection.execute(
            "SELECT details_json FROM download_jobs WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()

    assert scan is not None
    assert scan[0].startswith("TELEGRAPH_PREVIEW w1280 3p")
    assert scan[1] == "TELEGRAPH"
    payload = json.loads(details[0])
    assert payload["page_url"] == PAGE_URL
    assert payload["hosts"] == ["pic.example"]
    assert payload["image_count"] == 3


@pytest.mark.asyncio
async def test_a_short_preview_page_is_not_published(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=22)
    service = build_service(database, tmp_path, page_count=11)

    with pytest.raises(Exception) as excinfo:
        await service.download_for_candidate(candidate_id)

    assert excinfo.value.code == "TELEGRAPH_PAGE_COUNT_MISMATCH"
    assert "11/22" in excinfo.value.public_message
    with database._connect() as connection:  # noqa: SLF001
        artifacts = connection.execute(
            "SELECT COUNT(*) FROM artifacts"
        ).fetchone()
    assert artifacts[0] == 0


@pytest.mark.asyncio
async def test_the_page_count_gate_can_be_switched_off(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=22)
    service = build_service(
        database, tmp_path, page_count=11, require_match=False
    )

    result = await service.download_for_candidate(candidate_id)

    assert result.image_count == 11


@pytest.mark.asyncio
async def test_a_candidate_without_a_preview_link_fails_clearly(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (7, 'APPROVED')"
        )
    service = build_service(database, tmp_path, page_count=1)

    with pytest.raises(Exception) as excinfo:
        await service.download_for_candidate(7)

    assert excinfo.value.code == "TELEGRAPH_PAGE_UNREACHABLE"


def test_an_oversized_attachment_routes_to_the_preview_page(
    tmp_path: Path,
) -> None:
    """The whole point of the chain: a 138 MB book still gets a route.

    Bot API `getFile` would refuse the attachment permanently, so approval must
    not queue TELEGRAM for it.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_preview_candidate(database, attachment_bytes=138_700_000)
    )

    app = create_app(
        settings,
        telegraph_transport=build_transport(1),
        telegraph_resolver=public_resolver,
    )
    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": detail.context["csrf_token"]},
        )
        assert response.status_code == 303

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert [job.provider for job in jobs] == [PROVIDER_TELEGRAPH]


def test_a_small_attachment_still_wins_over_the_preview_page(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_preview_candidate(database, attachment_bytes=4096)
    )

    app = create_app(
        settings,
        telegraph_transport=build_transport(1),
        telegraph_resolver=public_resolver,
    )
    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": detail.context["csrf_token"]},
        )

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert [job.provider for job in jobs] == ["TELEGRAM"]


def test_the_manual_preview_button_queues_one_job(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_preview_candidate(database, attachment_bytes=4096)
    )

    app = create_app(
        settings,
        telegraph_transport=build_transport(1),
        telegraph_resolver=public_resolver,
    )
    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        csrf = detail.context["csrf_token"]
        assert "用预览页下载" in detail.text
        client.post(
            f"/candidates/{candidate_id}/approve", data={"csrf_token": csrf}
        )
        response = client.post(
            f"/candidates/{candidate_id}/telegraph",
            data={"csrf_token": csrf},
        )
        assert response.status_code == 303
        # Idempotent: pressing twice must not create a second job.
        client.post(
            f"/candidates/{candidate_id}/telegraph",
            data={"csrf_token": csrf},
        )

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert sorted(job.provider for job in jobs) == ["TELEGRAM", "TELEGRAPH"]


@pytest.mark.asyncio
async def test_a_page_count_mismatch_parks_the_candidate_in_needs_info(
    tmp_path: Path,
) -> None:
    """The candidate stays reviewable so the operator can add the second link."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=22)
    service = build_service(database, tmp_path, page_count=11)
    download_service = DownloadService(
        database,
        tmp_path / "work",
        telegraph_download=service.download_for_candidate,
    )
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    await download_service.enqueue_telegraph_download(candidate_id)

    assert await download_service._process_one() is True  # noqa: SLF001

    candidate = await database.get_candidate(candidate_id)
    jobs = await download_service.list_jobs_for_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "NEEDS_INFO"
    assert "11/22" in candidate.filter_reason
    assert jobs[0].error_code == "TELEGRAPH_PAGE_COUNT_MISMATCH"
    # Retryable, because supplying the missing link is what unblocks it.
    assert jobs[0].is_retryable is True


@pytest.mark.asyncio
async def test_the_worker_claims_a_telegraph_job(tmp_path: Path) -> None:
    """Locks the provider list: a new provider must not sit in PENDING."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=4)
    service = build_service(database, tmp_path, page_count=4)
    download_service = DownloadService(
        database,
        tmp_path / "work",
        telegraph_download=service.download_for_candidate,
    )
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    await download_service.enqueue_telegraph_download(candidate_id)

    assert await download_service._process_one() is True  # noqa: SLF001

    jobs = await download_service.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"
    candidate = await database.get_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "DOWNLOADED"


@pytest.mark.asyncio
async def test_the_preview_archive_converts_to_a_cbz_with_source_grade(
    tmp_path: Path,
) -> None:
    """The existing pipeline consumes the preview ZIP with no special casing."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_preview_candidate(database, pages=5)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO metadata_values "
            "(candidate_id, field_name, field_value, value_source) "
            "VALUES (?, 'Title', 'Preview Book', 'EXHENTAI')",
            (candidate_id,),
        )
    await build_service(
        database, tmp_path, page_count=5
    ).download_for_candidate(candidate_id)

    conversion = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'DOWNLOADED' WHERE id = ?",
            (candidate_id,),
        )
    await conversion.enqueue_for_candidate(candidate_id)
    assert await conversion._process_one() is True  # noqa: SLF001

    published = list((tmp_path / "library").glob("*.cbz"))
    assert len(published) == 1
    with zipfile.ZipFile(published[0]) as archive:
        comicinfo = archive.read("ComicInfo.xml").decode("utf-8")
    assert "<ScanInformation>TELEGRAPH_PREVIEW w1280 5p" in comicinfo