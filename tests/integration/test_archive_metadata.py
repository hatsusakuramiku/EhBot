"""R85: actual CBZ values, shared edits and safe in-place synchronization."""

import asyncio
import hashlib
import json
from pathlib import Path
import threading
from xml.etree import ElementTree as ET
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.archive.backends.zip_backend import rewrite_comicinfo
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.main import create_app
from app.review.models import editable_metadata_field
from app.review.service import ReviewService
from tests.work_commands import observed_work
from tests.integration.test_api_domains import seed, csrf_token
from tests.integration.test_api_v1 import make_settings, log_in


def packed_fixture(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, "DOWNLOADED", "Database title")])
    database = Database(settings.data_path / "ehbot.db")
    path = settings.library_path / "unchanged.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", (
            "<ComicInfo><Title>File title</Title><Series>File series</Series>"
            "<PageCount>2</PageCount><Manga>Yes</Manga><Added>2020-01-01</Added>"
            "<Translator>Original translator</Translator>"
            "<ScanInformation>Original quality</ScanInformation>"
            "<Pages><Page Image='0' Type='FrontCover'/></Pages></ComicInfo>"
        ))
        archive.writestr("001.jpg", b"first-image" * 10000)
        archive.writestr("002.jpg", b"second-image")
    with database.connection() as connection:
        # A published CBZ need not have a surviving original archive.
        connection.execute("INSERT INTO download_jobs (candidate_id, idempotency_key, provider, state) "
                           "VALUES (1, 'conversion:1', 'CONVERSION', 'CONVERSION_COMPLETED')")
        connection.execute("INSERT INTO artifacts (job_id, artifact_type, path, page_count) "
                           "VALUES (1, 'CBZ', ?, 2)", (str(path),))
    service = ArchivedWorkService(database, settings.library_path, settings.work_path)
    return settings, database, path, service


@pytest.mark.asyncio
async def test_edit_preserves_images_facts_and_extra_elements_and_updates_digest(tmp_path):
    _, database, path, service = await asyncio.to_thread(packed_fixture, tmp_path)
    mode = path.stat().st_mode
    await ReviewService(database).set_manual_metadata(1, "admin", "Series", "New series")
    result = await service.sync_archive_metadata(1)
    assert result["synced"] is True
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("ComicInfo.xml"))
        assert root.findtext("Series") == root.findtext("LocalizedSeries") == "New series"
        assert root.findtext("Title") == "File title"
        assert root.findtext("Added") == "2020-01-01"
        assert root.findtext("PageCount") == "2"
        assert root.findtext("ScanInformation") == "Original quality"
        assert root.findtext("Translator") == "Original translator"
        assert root.find("Pages/Page").attrib == {"Image": "0", "Type": "FrontCover"}
        assert archive.read("001.jpg") == b"first-image" * 10000
        assert archive.read("002.jpg") == b"second-image"
    with database.connection() as connection:
        artifact = connection.execute("SELECT path, sha256, size_bytes, page_count, job_id FROM artifacts").fetchone()
        audit = json.loads(connection.execute("SELECT details_json FROM review_actions ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert tuple(artifact) == (str(path), hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, 2, 1)
    assert audit["archive_synced"] is True
    assert list(path.parent.iterdir()) == [path]
    assert path.stat().st_mode == mode


@pytest.mark.asyncio
async def test_metadata_rewrite_does_not_hold_the_sqlite_write_lock(tmp_path, monkeypatch):
    _, database, path, service = await asyncio.to_thread(packed_fixture, tmp_path)
    await ReviewService(database).set_manual_metadata(1, "admin", "Series", "New series")
    before = path.read_bytes()
    entered = threading.Event()
    release = threading.Event()
    original = __import__("app.downloads.archived", fromlist=["rewrite_comicinfo"]).rewrite_comicinfo

    def blocked(path_arg, comicinfo):
        entered.set()
        assert release.wait(3)
        return original(path_arg, comicinfo)

    monkeypatch.setattr("app.downloads.archived.rewrite_comicinfo", blocked)
    task = asyncio.create_task(service.sync_archive_metadata(1))
    assert await asyncio.to_thread(entered.wait, 3)

    # A concurrent metadata edit must be able to commit while the ZIP is being
    # rebuilt. The final identity check then rejects the stale file and restores
    # the original bytes.
    await ReviewService(database).set_manual_metadata(1, "admin", "Series", "Concurrent series")
    release.set()
    result = await asyncio.wait_for(task, 3)
    assert result["reason"] == "WORK_CHANGED"
    assert path.read_bytes() == before


@pytest.mark.parametrize("status,job_state,reason", [
    ("DOWNLOADED", "CONVERSION_RUNNING", "WORK_PACK_RUNNING"),
    ("REJECTED", "CONVERSION_COMPLETED", "WORK_PACK_FORBIDDEN"),
    ("PENDING_REVIEW", "CONVERSION_COMPLETED", "WORK_PACK_FORBIDDEN"),
    ("NEEDS_INFO", "CONVERSION_COMPLETED", "WORK_PACK_FORBIDDEN"),
])
@pytest.mark.asyncio
async def test_sync_guards_preserve_existing_bytes(tmp_path, status, job_state, reason):
    _, database, path, service = await asyncio.to_thread(packed_fixture, tmp_path)
    before = path.read_bytes()
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET status = ?", (status,))
        connection.execute("UPDATE download_jobs SET state = ?", (job_state,))
    result = await service.sync_archive_metadata(1)
    assert result["synced"] is False
    assert result["reason"] == reason
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_unpacked_work_previews_fields_and_keeps_edits_in_database(tmp_path):
    _, database, path, service = await asyncio.to_thread(packed_fixture, tmp_path)
    with database.connection() as connection:
        connection.execute("DELETE FROM artifacts")
    await ReviewService(database).set_manual_metadata(1, "admin", "Translator", "New translator")
    before = path.read_bytes()
    result = await service.sync_archive_metadata(1)
    assert result["reason"] == "WORK_NOT_PACKED"
    snapshot = await service.archive_metadata(1)
    assert snapshot["packaged"] is False
    translator = next(e for e in snapshot["elements"] if e["name"] == "Translator")
    assert translator["field_value"] == "New translator"
    assert not translator["in_file"]
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_corrupt_file_reports_unsynced_database_edit(tmp_path):
    _, database, path, service = await asyncio.to_thread(packed_fixture, tmp_path)
    path.write_bytes(b"broken zip")
    await ReviewService(database).set_manual_metadata(1, "admin", "Title", "Saved title")
    result = await service.sync_archive_metadata(1)
    assert result["reason"] == "ARCHIVE_METADATA_SYNC_FAILED"
    assert (await database.effective_metadata(1))["Title"] == "Saved title"
    assert path.read_bytes() == b"broken zip"
    assert (await service.archive_metadata(1))["error"]


def test_streaming_copy_failure_leaves_original_cbz(tmp_path, monkeypatch):
    _, _, path, _ = packed_fixture(tmp_path)
    original = path.read_bytes()
    def fail(*args):
        raise OSError("simulated copy failure")
    monkeypatch.setattr("app.archive.backends.zip_backend.shutil.copyfileobj", fail)
    with pytest.raises(OSError):
        rewrite_comicinfo(path, b"<ComicInfo/>")
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.asyncio
async def test_sync_refuses_artifact_outside_current_library_root(tmp_path):
    _, database, path, service = await asyncio.to_thread(packed_fixture, tmp_path)
    before = path.read_bytes()
    service._library_path = tmp_path / "other-library"
    assert (await service.sync_archive_metadata(1))["reason"] == "ARCHIVE_METADATA_SYNC_FAILED"
    assert path.read_bytes() == before
    assert (await service.archive_metadata(1))["error"]


@pytest.mark.parametrize("name", ["Pages", "PageCount", "Added", "Manga", "TAG", "TagsRaw", "FileSize", "bad-name", "1bad", "A" * 65])
def test_generated_raw_and_invalid_fields_are_not_editable(name):
    assert not editable_metadata_field(name)


def test_page_and_api_read_real_values_and_share_archive_edits(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        before = client.get("/api/v1/works/1").json()["archive_metadata"]
        title = next(e for e in before["elements"] if e["name"] == "Title")
        assert title["field_value"] == "File title"
        assert title["file_value"] == "File title"
        page = client.get("/works/1")
        assert "文件中的旧值" not in page.text
        assert 'data-archive-element="PageCount"' in page.text
        assert 'name="field_name" value="PageCount"' not in page.text
        token = csrf_token(client)
        edited = client.patch("/api/v1/works/1/metadata", headers={CSRF_HEADER: token},
                              json={**observed_work(client), "fields": {"Series": "New series", "Translator": "New translator", "ShelfLabel": "Private label"}})
        assert edited.status_code == 200
        assert edited.json()["archive_synced"] is True
        assert edited.json()["sync_skipped"] is None
        assert "repacked" not in edited.json()
        assert edited.json()["metadata"]["Translator"] == "New translator"
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("ComicInfo.xml"))
            assert root.findtext("Series") == "New series"
            assert root.findtext("Translator") == "New translator"
            assert root.find("ShelfLabel") is None
        posted = client.post("/candidates/1/metadata", follow_redirects=False,
                             data={**observed_work(client), "csrf_token": token, "field_name": "Summary", "field_value": "Updated summary"})
        assert posted.status_code == 303
        page = client.get(posted.headers["location"])
        assert "归档文件已同步" in page.text
        assert "仅数据库，不写入归档" in page.text
        with zipfile.ZipFile(path) as archive:
            assert ET.fromstring(archive.read("ComicInfo.xml")).findtext("Summary") == "Updated summary"
        assert client.patch("/api/v1/works/1/metadata", json={"fields": {"Translator": "No CSRF"}}).status_code == 403


def test_custom_page_size_fetches_more_than_200_and_keeps_repeated_filters(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(i, "DOWNLOADED", f"Book {i}") for i in range(1, 302)])
    database = Database(settings.data_path / "ehbot.db")
    with database.connection() as connection:
        for candidate_id in range(1, 302):
            connection.execute("INSERT INTO download_jobs (candidate_id, idempotency_key, provider, state) VALUES (?, ?, 'TELEGRAM', 'COMPLETED')",
                               (candidate_id, f"seed:{candidate_id}"))
            connection.execute("INSERT INTO artifacts (job_id, artifact_type, path) VALUES (?, 'ARCHIVE', ?)",
                               (candidate_id, str(settings.work_path / f"{candidate_id}.zip")))
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        for url in ("/candidates/all", "/downloaded"):
            response = client.get(url + "?page_size=300")
            assert response.status_code == 200
            assert response.context["page_size"] == 300
            assert 'value="300"' in response.text
            for invalid in ("0", "abc", ""):
                assert client.get(url + "?page_size=" + invalid).context["page_size"] == 50
        assert len(client.get("/api/v1/candidates?tab=all&page_size=300").json()["items"]) == 300
        assert len(client.get("/api/v1/downloaded?page_size=300").json()["works"]) == 300
        assert client.get("/api/v1/candidates?page_size=abc").json()["page_size"] == 50
        page = client.get("/candidates/all?tags=one&tags=two&sort=oldest&page=3&page_size=300")
        assert page.context["page_size_form_fields"] == [("tags", "one"), ("tags", "two"), ("sort", "oldest")]
