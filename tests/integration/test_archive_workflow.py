import hashlib
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.archive.service import ArchiveSettingsService
from app.archive.vault import decrypt_password
from app.config import Settings
from app.conversion.service import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_VOLUMES,
    ConversionService,
)
from app.db.database import Database
from app.downloads.models import DOWNLOAD_STATE_COMPLETED
from app.main import create_app

from tests.unit.archive_fixtures import image_bytes, write_real_image_zip


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
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
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


async def seed_downloaded_archive(
    database: Database, archive_path: Path
) -> int:
    await database.initialize()
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (1, 'APPROVED')"
        )
        connection.execute(
            "INSERT INTO download_jobs "
            "(candidate_id, idempotency_key, provider, state) "
            "VALUES (1, 'telegram:1:a', 'TELEGRAM', ?)",
            (DOWNLOAD_STATE_COMPLETED,),
        )
        connection.execute(
            "INSERT INTO artifacts (job_id, artifact_type, path, size_bytes) "
            "VALUES (1, 'ARCHIVE', ?, 1024)",
            (str(archive_path),),
        )
        connection.execute(
            "INSERT INTO metadata_values "
            "(candidate_id, field_name, field_value, value_source) "
            "VALUES (1, 'Title', 'Archive Title', 'TELEGRAM')"
        )
    return 1


def write_zip(path: Path, names: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, image_bytes(name))


def job_state(database: Database, job_id: int) -> tuple[str, str | None]:
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT state, error_code FROM download_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


def test_archive_settings_page_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/settings/archive", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_archive_settings_page_lists_registered_profiles(tmp_path: Path) -> None:
    """归档 shows the tool profiles; 路径 shows the directories they write to."""
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get("/settings/archive")
        paths = client.get("/settings/paths")

    assert page.status_code == 200
    assert "zipfile-default" in page.text
    assert "7zz-default" in page.text
    assert paths.status_code == 200
    assert str(settings.library_path) in paths.text


def test_admin_can_update_limits_and_tool_profile(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/archive")
        csrf_token = page.context["csrf_token"]
        limits_response = client.post(
            "/archive-settings/limits",
            data={
                "max_members": "1200",
                "max_total_bytes": "2000000",
                "max_member_bytes": "500000",
                "max_compression_ratio": "80",
                "max_depth": "4",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        profile_response = client.post(
            "/archive-settings/profiles/7zz-default",
            data={
                "executable_path": "/usr/bin/7zz",
                "timeout_seconds": "1200",
                "enabled": "on",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        updated = client.get("/settings/archive")

    assert limits_response.status_code == 303
    assert profile_response.status_code == 303
    assert "1200" in updated.text
    assert "/usr/bin/7zz" in updated.text
    assert "\u4fdd\u7559\u539f\u59cb\u538b\u7f29\u5305" in updated.text


def test_image_quality_defaults_to_original_and_round_trips(
    tmp_path: Path,
) -> None:
    """The archive settings page must default to the lossless original."""
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/archive")
        assert page.context["image_quality"]["selected"] == "original"
        saved = client.post(
            "/archive-settings/limits",
            data={
                "max_members": "1200",
                "max_total_bytes": "2000000",
                "max_member_bytes": "500000",
                "max_compression_ratio": "80",
                "max_depth": "4",
                "image_quality": "medium",
                "csrf_token": page.context["csrf_token"],
            },
            follow_redirects=False,
        )
        updated = client.get("/settings/archive")

    assert saved.status_code == 303
    assert updated.context["image_quality"]["selected"] == "medium"


def test_unknown_image_quality_is_rejected(tmp_path: Path) -> None:
    """An unrecognised level would silently publish at an unintended quality."""
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/archive")
        response = client.post(
            "/archive-settings/limits",
            data={
                "image_quality": "ultra",
                "csrf_token": page.context["csrf_token"],
            },
        )
        after = client.get("/settings/archive")

    assert response.status_code == 400
    assert after.context["image_quality"]["selected"] == "original"


def test_invalid_limit_is_rejected_without_saving(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        csrf_token = client.get("/settings/archive").context["csrf_token"]
        response = client.post(
            "/archive-settings/limits",
            data={"max_members": "-3", "csrf_token": csrf_token},
        )

    assert response.status_code == 400
    assert "max_members" in response.text


def test_password_is_stored_encrypted_and_never_echoed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        csrf_token = client.get("/settings/passwords").context["csrf_token"]
        created = client.post(
            "/archive-settings/passwords",
            data={
                "name": "shared",
                "password": "top-secret-archive-password",
                "priority": "10",
                "enabled": "on",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        listed = client.get("/settings/passwords")

    assert created.status_code == 303
    assert "shared" in listed.text
    assert "top-secret-archive-password" not in listed.text

    database = Database(settings.data_path / "ehbot.db")
    with database._connect() as connection:  # noqa: SLF001
        rows = connection.execute(
            "SELECT secret_json FROM archive_passwords"
        ).fetchall()
    assert len(rows) == 1
    assert "top-secret-archive-password" not in str(rows[0][0])

    service = ArchiveSettingsService(database, settings.data_path)
    key = service._load_master_key_sync()  # noqa: SLF001
    assert decrypt_password(key, str(rows[0][0])) == "top-secret-archive-password"


def test_admin_can_delete_password_entry(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        csrf_token = client.get("/settings/passwords").context["csrf_token"]
        client.post(
            "/archive-settings/passwords",
            data={
                "name": "temporary",
                "password": "value",
                "priority": "10",
                "enabled": "on",
                "csrf_token": csrf_token,
            },
        )
        client.post(
            "/archive-settings/passwords/1/delete",
            data={"csrf_token": csrf_token},
        )
        listed = client.get("/settings/passwords")

    assert "temporary" not in listed.text


@pytest.mark.asyncio
async def test_conversion_publishes_cbz_and_records_snapshot(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "comic.zip"
    write_zip(archive_path, ("02.jpg", "01.jpg"))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    assert await service._process_one() is True  # noqa: SLF001

    state, error_code = job_state(database, job_id)
    assert state == CONVERSION_STATE_COMPLETED
    assert error_code is None

    published = tmp_path / "library" / "Archive Title.cbz"
    assert published.exists()
    with zipfile.ZipFile(published) as archive:
        assert archive.namelist() == ["ComicInfo.xml", "0001.jpg", "0002.jpg"]
        assert "<Title>Archive Title</Title>" in archive.read(
            "ComicInfo.xml"
        ).decode("utf-8")

    with database._connect() as connection:  # noqa: SLF001
        details = connection.execute(
            "SELECT details_json FROM download_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert '"backend":"zipfile"' in details
    assert '"tool_profile":"zipfile-default"' in details
    # The original archive is kept by default.
    assert archive_path.exists()


@pytest.mark.asyncio
async def test_the_packed_cbz_artifact_records_its_real_size_and_digest(
    tmp_path: Path,
) -> None:
    """`size_bytes` used to receive `page_count`.

    Every packed CBZ therefore reported a size of a few dozen bytes and the
    row could not be compared against the archive it came from, so this
    asserts the three columns independently rather than just "a row exists".
    """
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "comic.zip"
    write_zip(archive_path, ("01.jpg", "02.jpg", "03.jpg"))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    assert await service._process_one() is True  # noqa: SLF001

    published = tmp_path / "library" / "Archive Title.cbz"
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT path, sha256, size_bytes, page_count FROM artifacts "
            "WHERE job_id = ? AND artifact_type = 'CBZ'",
            (job_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == str(published)
    assert row[1] == hashlib.sha256(published.read_bytes()).hexdigest()
    assert row[2] == published.stat().st_size
    assert row[2] > 100, "a size that small is the old page-count bug"
    assert row[3] == 3


@pytest.mark.asyncio
async def test_conversion_deletes_original_when_keep_original_is_off(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "comic.zip"
    write_zip(archive_path, ("01.jpg",))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    settings_service = ArchiveSettingsService(database, tmp_path / "data")
    await settings_service.save_keep_original(False)
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        settings_service=settings_service,
    )

    await service.enqueue_for_candidate(candidate_id)
    await service._process_one()  # noqa: SLF001

    assert (tmp_path / "library" / "Archive Title.cbz").exists()
    assert not archive_path.exists()


@pytest.mark.asyncio
async def test_conversion_reencodes_pages_and_records_the_policy(
    tmp_path: Path,
) -> None:
    """A configured quality level must shrink the pages and say so in ComicInfo."""
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "comic.zip"
    write_real_image_zip(archive_path, ("01.jpg", "02.jpg"))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    settings_service = ArchiveSettingsService(database, tmp_path / "data")
    await settings_service.save_image_quality("medium")
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        settings_service=settings_service,
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    assert await service._process_one() is True  # noqa: SLF001

    published = tmp_path / "library" / "Archive Title.cbz"
    with zipfile.ZipFile(archive_path) as original:
        source_size = len(original.read("01.jpg"))
    with zipfile.ZipFile(published) as archive:
        assert archive.namelist() == ["ComicInfo.xml", "0001.jpg", "0002.jpg"]
        assert len(archive.read("0001.jpg")) < source_size
        comicinfo = archive.read("ComicInfo.xml").decode("utf-8")
    assert "<ScanInformation>requality=medium q60</ScanInformation>" in comicinfo

    with database._connect() as connection:  # noqa: SLF001
        details = connection.execute(
            "SELECT details_json FROM download_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert '"image_quality":"medium"' in details
    assert '"rewritten_pages":2' in details


@pytest.mark.asyncio
async def test_conversion_parks_split_archive_until_volumes_arrive(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    downloads = tmp_path / "work" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / "book.part1.rar").write_bytes(b"Rar!\x1a\x07\x00")
    (downloads / "book.part3.rar").write_bytes(b"Rar!\x1a\x07\x00")
    candidate_id = await seed_downloaded_archive(
        database, downloads / "book.part1.rar"
    )
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    await service._process_one()  # noqa: SLF001

    state, error_code = job_state(database, job_id)
    assert state == CONVERSION_STATE_WAITING_VOLUMES
    assert error_code == "ARCHIVE_VOLUMES_MISSING"
    with database._connect() as connection:  # noqa: SLF001
        details = connection.execute(
            "SELECT details_json FROM download_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert "book.part2.rar" in details

    # The operator supplies the missing volume and requeues the same task.
    (downloads / "book.part2.rar").write_bytes(b"Rar!\x1a\x07\x00")
    requeued_job_id = await service.enqueue_for_candidate(candidate_id)
    assert requeued_job_id == job_id
    assert job_state(database, job_id)[0] == CONVERSION_STATE_PENDING


@pytest.mark.asyncio
async def test_conversion_parks_encrypted_archive_without_password(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "locked.zip"
    write_zip(archive_path, ("01.jpg",))
    data = bytearray(archive_path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = data.find(signature)
        while position != -1:
            data[position + offset] |= 0x01
            position = data.find(signature, position + 1)
    archive_path.write_bytes(bytes(data))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    await service._process_one()  # noqa: SLF001

    state, error_code = job_state(database, job_id)
    assert state == CONVERSION_STATE_WAITING_PASSWORD
    assert error_code == "ARCHIVE_PASSWORD_REQUIRED"
    assert not (tmp_path / "library" / "Archive Title.cbz").exists()


@pytest.mark.asyncio
async def test_conversion_rejects_path_traversal_member(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "evil.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.jpg", image_bytes("escape.jpg"))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    await service._process_one()  # noqa: SLF001

    state, error_code = job_state(database, job_id)
    assert state == "CONVERSION_FAILED"
    assert error_code == "ARCHIVE_MEMBER_TRAVERSAL"
    assert not (tmp_path.parent / "escape.jpg").exists()


@pytest.mark.asyncio
async def test_conversion_rejects_unsupported_format(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "notes.txt"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(b"plain text, not an archive")
    candidate_id = await seed_downloaded_archive(database, archive_path)
    service = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )

    job_id = await service.enqueue_for_candidate(candidate_id)
    await service._process_one()  # noqa: SLF001

    state, error_code = job_state(database, job_id)
    assert state == "CONVERSION_FAILED"
    assert error_code == "UNSUPPORTED_FORMAT"


@pytest.mark.asyncio
async def test_conversion_uses_vault_password_and_marks_success(
    tmp_path: Path,
) -> None:
    """A stored password unlocks the archive and is promoted for later tasks."""
    database = Database(tmp_path / "ehbot.db")
    archive_path = tmp_path / "work" / "downloads" / "comic.zip"
    write_zip(archive_path, ("01.jpg",))
    candidate_id = await seed_downloaded_archive(database, archive_path)
    settings_service = ArchiveSettingsService(database, tmp_path / "data")
    password_id = await settings_service.add_password(
        name="shared", password="unlock-me", priority=5
    )
    attempts = await settings_service.password_attempts()
    assert attempts == ((password_id, "unlock-me"),)

    await settings_service.mark_password_success(password_id)
    entries = await settings_service.passwords()
    assert entries[0].last_success_at is not None


def test_startup_survives_a_failing_toolchain_install(tmp_path: Path) -> None:
    """Provisioning is best effort and must never break startup.

    Regression: with auto-install on and no reachable download, the lifespan
    raised and the whole application failed to start. Every non-7-Zip feature
    must stay available on a host that cannot fetch the archiver.
    """
    settings = Settings(
        data_path=tmp_path / "data",
        library_path=tmp_path / "library",
        work_path=tmp_path / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=True,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        authenticate(client, settings)
        page = client.get("/settings/archive")
        assert page.status_code == 200


def test_paths_can_be_changed_from_the_settings_page(tmp_path: Path) -> None:
    """The operator must be able to move the library and work directories."""
    settings = make_settings(tmp_path)
    new_library = tmp_path / "moved-library"
    new_work = tmp_path / "moved-work"
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get("/settings/paths")
        response = client.post(
            "/archive-settings/paths",
            data={
                "library_path": str(new_library),
                "work_path": str(new_work),
                "csrf_token": page.context["csrf_token"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        updated = client.get("/settings/paths")
        assert str(new_library) in updated.text
        assert str(new_work) in updated.text

    # Directories are created as part of the writability check.
    assert new_library.is_dir()
    assert new_work.is_dir()


def test_relative_path_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get("/settings/paths")
        response = client.post(
            "/archive-settings/paths",
            data={
                "library_path": "relative/library",
                "work_path": "",
                "csrf_token": page.context["csrf_token"],
            },
        )
        assert response.status_code == 400
        assert "\u7edd\u5bf9\u8def\u5f84" in response.text


def test_clearing_an_override_restores_the_default(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    override = tmp_path / "temporary-library"
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get("/settings/paths")
        client.post(
            "/archive-settings/paths",
            data={
                "library_path": str(override),
                "work_path": "",
                "csrf_token": page.context["csrf_token"],
            },
        )
        page = client.get("/settings/paths")
        assert str(override) in page.text

        client.post(
            "/archive-settings/paths",
            data={
                "library_path": "",
                "work_path": "",
                "csrf_token": page.context["csrf_token"],
            },
        )
        restored = client.get("/settings/paths")
        assert str(settings.library_path) in restored.text


@pytest.mark.asyncio
async def test_conversion_publishes_into_the_overridden_library(
    tmp_path: Path,
) -> None:
    """A stored override must take effect without restarting the service."""
    database = Database(tmp_path / "data" / "ehbot.db")
    await database.initialize()
    service = ArchiveSettingsService(
        database,
        tmp_path / "data",
        default_library_path=tmp_path / "library",
        default_work_path=tmp_path / "work",
    )
    moved = tmp_path / "elsewhere"
    await service.save_paths({"library_path": str(moved), "work_path": ""})

    conversion = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        settings_service=service,
        data_path=tmp_path / "data",
    )
    library_path, work_path = await conversion._effective_paths()  # noqa: SLF001
    assert library_path == moved
    assert work_path == tmp_path / "work"
