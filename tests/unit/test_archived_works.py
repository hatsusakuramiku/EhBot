"""The 已下载内容 service: remove, re-download, rename (R10).

These drive `ArchivedWorkService` against a real SQLite database and a real
temporary library, because what the service has to get right is the interaction
between the two: a record deleted whose file survived, a file refused because it
sits outside the library, an empty directory left behind after a move. A fake
filesystem would prove none of that.

The download worker is not running here -- `create_app` is never called -- so
these tests may seed any state they like, including PENDING.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest

from app.db.database import Database
from app.downloads.archived import ArchivedWorkError, ArchivedWorkService
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_RUNNING,
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_PENDING,
    PROVIDER_CONVERSION,
    PROVIDER_TELEGRAM,
)


#: `download_jobs.idempotency_key` is UNIQUE and Windows' clock is too coarse to
#: separate two rows seeded back to back, so the keys come from a counter.
_KEYS = itertools.count(1)


class Fixture:
    """A database, a library, a work directory, and the service over them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.library = root / "library"
        self.work = root / "work"
        self.library.mkdir(parents=True)
        self.work.mkdir(parents=True)
        self.database = Database(root / "ehbot.db")
        asyncio.run(self.database.initialize())
        self.notified: list[int] = []
        self.repacked: list[int] = []
        self.service = ArchivedWorkService(
            self.database,
            self.library,
            self.work,
            conversion_enqueue=self._enqueue,
            notify=self.notified.append,
        )

    async def _enqueue(self, candidate_id: int) -> int:
        self.repacked.append(candidate_id)
        return 0

    # ------------------------------------------------------------- seeding

    def candidate(self, *, title: str = "示例作品") -> int:
        with self.database._connect() as connection:  # noqa: SLF001
            cursor = connection.execute(
                "INSERT INTO candidates (status) VALUES ('DOWNLOADED')"
            )
            candidate_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source, "
                " confidence, is_manual) "
                "VALUES (?, 'Title', ?, 'MANUAL', 1.0, 1)",
                (candidate_id, title),
            )
        return candidate_id

    def job(
        self,
        candidate_id: int,
        *,
        state: str = DOWNLOAD_STATE_COMPLETED,
        provider: str = PROVIDER_TELEGRAM,
        key: str | None = None,
    ) -> int:
        with self.database._connect() as connection:  # noqa: SLF001
            cursor = connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, details_json) "
                "VALUES (?, ?, ?, ?, '{}')",
                (
                    candidate_id,
                    key or f"archived:{next(_KEYS)}",
                    provider,
                    state,
                ),
            )
            return int(cursor.lastrowid)

    def artifact(self, job_id: int, *, kind: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")
        with self.database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO artifacts (job_id, artifact_type, path, size_bytes)"
                " VALUES (?, ?, ?, 7)",
                (job_id, kind, str(path)),
            )

    def packaged(
        self,
        *,
        pack_state: str = CONVERSION_STATE_COMPLETED,
        archive_name: str = "source.zip",
        cbz_relative: str = "作者/示例作品.cbz",
    ) -> tuple[int, Path, Path]:
        """A downloaded, packaged work: archive on disk and CBZ in the library."""
        candidate_id = self.candidate()
        job_id = self.job(candidate_id)
        archive = self.work / archive_name
        self.artifact(job_id, kind="ARCHIVE", path=archive)
        pack_id = self.job(
            candidate_id,
            state=pack_state,
            provider=PROVIDER_CONVERSION,
            key=f"convert:{candidate_id}",
        )
        cbz = self.library / cbz_relative
        self.artifact(pack_id, kind="CBZ", path=cbz)
        return candidate_id, archive, cbz

    # ------------------------------------------------------------ reading

    def audit(self) -> list[tuple]:
        with self.database._connect() as connection:  # noqa: SLF001
            return [
                tuple(row)
                for row in connection.execute(
                    "SELECT candidate_id, deleted_files, operator_name, "
                    "cbz_path FROM removed_works ORDER BY id"
                )
            ]

    def jobs(self, candidate_id: int) -> list[tuple[str, int]]:
        with self.database._connect() as connection:  # noqa: SLF001
            return [
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT state, attempt_count FROM download_jobs "
                    "WHERE candidate_id = ? ORDER BY id",
                    (candidate_id,),
                )
            ]

    def candidate_rows(self) -> int:
        with self.database._connect() as connection:  # noqa: SLF001
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM candidates"
                ).fetchone()[0]
            )


@pytest.fixture()
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


class TestRemoval:
    def test_a_removal_keeps_the_files_unless_it_is_asked_not_to(
        self, fixture: Fixture
    ) -> None:
        """EHBot.md §1.2.3:「默认不移除已下载文件」.

        The default is the whole point. A caller that forgets the flag must
        delete nothing, so the safe outcome is the one that happens by omission.
        """
        candidate_id, archive, cbz = fixture.packaged()

        result = asyncio.run(fixture.service.remove_work(candidate_id))

        assert result["deleted_files"] == ()
        assert archive.exists()
        assert cbz.exists()
        # Both job rows go: the download and its packaging task are two halves
        # of one book, and leaving the packer's row would leave the page showing
        # a CBZ whose download it can no longer find.
        assert fixture.jobs(candidate_id) == []
        assert fixture.audit() == [(candidate_id, 0, "admin", str(cbz))]

    def test_deleting_the_files_takes_both_and_prunes_the_directory(
        self, fixture: Fixture
    ) -> None:
        candidate_id, archive, cbz = fixture.packaged()
        parent = cbz.parent

        result = asyncio.run(
            fixture.service.remove_work(
                candidate_id, delete_files=True, operator_name="operator"
            )
        )

        assert set(result["deleted_files"]) == {str(archive), str(cbz)}
        assert not archive.exists()
        assert not cbz.exists()
        # The 「作者」 directory existed only to hold that book. Leaving it turns
        # a library into a tree of empty folders after a few removals.
        assert not parent.exists()
        # ...but never the root itself, which the operator configured.
        assert fixture.library.exists()
        assert fixture.audit() == [(candidate_id, 1, "operator", str(cbz))]

    def test_a_file_outside_the_library_is_refused_and_survives(
        self, fixture: Fixture
    ) -> None:
        """The stored path is not trusted even though this service wrote it.

        A path can reach `artifacts.path` from an older layout template, from a
        library directory that has since been re-pointed, or from an ExHentai
        artist name that walked out of the tree. Deleting whatever the column
        says would make a settings mistake unrecoverable.
        """
        candidate_id, _, _ = fixture.packaged()
        outside = fixture.root / "outside.cbz"
        outside.write_bytes(b"not ours")
        with fixture.database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE artifacts SET path = ? WHERE artifact_type = 'CBZ'",
                (str(outside),),
            )

        result = asyncio.run(
            fixture.service.remove_work(candidate_id, delete_files=True)
        )

        assert result["failed_files"] == (str(outside),)
        assert outside.exists()
        # The records still go -- the operator asked for the work to be removed,
        # and a refused file is reported rather than aborting the removal.
        assert fixture.jobs(candidate_id) == []
        # `deleted_files` is 0 because not everything the operator asked to
        # delete was deleted. A 1 here would be the audit trail lying.
        assert fixture.audit()[0][1] == 0

    def test_an_already_missing_file_is_the_desired_end_state(
        self, fixture: Fixture
    ) -> None:
        """A file somebody deleted by hand is not a failed removal.

        It is reported as gone rather than as refused, which is the honest
        answer: the operator asked for it not to be there. Treating it as a
        failure would also flip the audit row's `deleted_files` to 0 and leave
        the record claiming the bytes might still be on disk.
        """
        candidate_id, archive, cbz = fixture.packaged()
        cbz.unlink()

        result = asyncio.run(
            fixture.service.remove_work(candidate_id, delete_files=True)
        )

        assert result["failed_files"] == ()
        assert set(result["deleted_files"]) == {str(cbz), str(archive)}
        assert not archive.exists()
        assert fixture.audit()[0][1] == 1

    def test_the_candidate_and_its_history_survive_the_removal(
        self, fixture: Fixture
    ) -> None:
        """Removing downloaded content is not forgetting the book.

        The candidate carries the metadata and every `review_actions` row points
        at it, so deleting it would orphan the audit trail and lose the reason the
        book was approved in the first place.
        """
        candidate_id, _, _ = fixture.packaged()
        asyncio.run(
            fixture.database.record_review_action(
                candidate_id, "APPROVE", "admin", {}
            )
        )

        asyncio.run(fixture.service.remove_work(candidate_id))

        assert fixture.candidate_rows() == 1
        actions = asyncio.run(
            fixture.database.list_review_actions(candidate_id)
        )
        assert [entry.action for entry in actions] == ["APPROVE"]

    def test_a_work_with_a_task_in_flight_is_refused_not_raced(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()
        with fixture.database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE download_jobs SET state = ? "
                "WHERE candidate_id = ? AND provider <> ?",
                (DOWNLOAD_STATE_PENDING, candidate_id, PROVIDER_CONVERSION),
            )

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(fixture.service.remove_work(candidate_id))

        assert raised.value.code == "WORK_STILL_RUNNING"
        assert fixture.audit() == []

    def test_a_work_being_packed_right_now_is_refused(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged(
            pack_state=CONVERSION_STATE_RUNNING
        )

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(fixture.service.remove_work(candidate_id))

        assert raised.value.code == "WORK_PACK_RUNNING"

    def test_a_work_with_no_archive_cannot_be_removed(
        self, fixture: Fixture
    ) -> None:
        candidate_id = fixture.candidate()
        fixture.job(candidate_id)

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(fixture.service.remove_work(candidate_id))

        assert raised.value.code == "WORK_NOT_DOWNLOADED"


class TestRedownload:
    def test_the_original_row_is_reused_and_the_attempt_counted(
        self, fixture: Fixture
    ) -> None:
        """`idempotency_key` is UNIQUE per source, so a second row is impossible.

        Reusing it is also what keeps one book's attempt history in one place:
        an operator who has re-fetched a book five times sees `attempt_count`
        say so.
        """
        candidate_id, _, _ = fixture.packaged()

        result = asyncio.run(fixture.service.redownload_work(candidate_id))

        assert result["repack"] is False
        download, _pack = fixture.jobs(candidate_id)
        assert download == (DOWNLOAD_STATE_PENDING, 1)
        assert fixture.notified == [candidate_id]
        assert fixture.repacked == []

    def test_repack_is_opt_in_and_queues_the_packer_behind_the_download(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()

        result = asyncio.run(
            fixture.service.redownload_work(candidate_id, repack=True)
        )

        assert result["repack"] is True
        assert fixture.repacked == [candidate_id]

    def test_a_work_already_downloading_is_not_queued_twice(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()
        asyncio.run(fixture.service.redownload_work(candidate_id))

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(fixture.service.redownload_work(candidate_id))

        assert raised.value.code == "WORK_STILL_RUNNING"
        # And the first request's row is untouched by the refusal.
        assert fixture.jobs(candidate_id)[0] == (DOWNLOAD_STATE_PENDING, 1)

    def test_a_work_being_re_downloaded_is_still_findable(
        self, fixture: Fixture
    ) -> None:
        """The reason `downloaded_work` does not filter on COMPLETED.

        Its archive is from the previous run and its job row is PENDING again.
        Filtering the single-work read on COMPLETED would make it invisible, and
        the guard above would be unreachable -- the operator would be told the
        work has no archive, which is both wrong and unactionable.
        """
        candidate_id, _, _ = fixture.packaged()
        asyncio.run(fixture.service.redownload_work(candidate_id))

        work = asyncio.run(fixture.database.downloaded_work(candidate_id))
        assert work is not None
        assert work.state == DOWNLOAD_STATE_PENDING
        # The list is filtered, though: a book being re-fetched is not finished
        # content, so it drops off the page until the download lands.
        works, total = asyncio.run(fixture.database.list_downloaded_works())
        assert (works, total) == ([], 0)


class TestRename:
    def test_a_rename_moves_the_file_and_pins_where_a_repack_lands(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, cbz = fixture.packaged()
        old_parent = cbz.parent

        result = asyncio.run(
            fixture.service.rename_work(
                candidate_id, filename="新名字", directory="分类/作者"
            )
        )

        assert result["moved"] is True
        assert result["relative_path"] == "分类/作者/新名字.cbz"
        moved = fixture.library / "分类" / "作者" / "新名字.cbz"
        assert moved.exists()
        assert not cbz.exists()
        # The directory the book left is pruned, for the reason a removal prunes.
        assert not old_parent.exists()
        work = asyncio.run(fixture.database.downloaded_work(candidate_id))
        assert work is not None
        assert work.cbz_path == str(moved)
        # The pin is the point of the action: without it the next repack would
        # re-render the path from the template and undo the move.
        assert work.library_relative_path == "分类/作者/新名字.cbz"

    def test_a_cbz_suffix_the_operator_typed_is_not_doubled(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()

        result = asyncio.run(
            fixture.service.rename_work(candidate_id, filename="书名.cbz")
        )

        assert result["relative_path"] == "书名.cbz"

    def test_submitting_the_current_name_is_not_an_error(
        self, fixture: Fixture
    ) -> None:
        """Refusing would read as the rename being broken."""
        candidate_id, _, cbz = fixture.packaged()

        result = asyncio.run(
            fixture.service.rename_work(
                candidate_id, filename=cbz.stem, directory="作者"
            )
        )

        assert result["moved"] is False
        assert cbz.exists()

    @pytest.mark.parametrize("directory", ["..", "../../etc", "分类/../.."])
    def test_a_directory_cannot_walk_out_of_the_library(
        self, fixture: Fixture, directory: str
    ) -> None:
        candidate_id, _, cbz = fixture.packaged()

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.rename_work(
                    candidate_id, filename="x", directory=directory
                )
            )

        assert raised.value.code == "DIRECTORY_INVALID"
        assert cbz.exists()

    def test_a_filename_of_nothing_but_illegal_characters_is_refused(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(fixture.service.rename_work(candidate_id, filename="///"))

        assert raised.value.code == "FILENAME_INVALID"

    def test_an_unpacked_work_has_no_filename_to_change(
        self, fixture: Fixture
    ) -> None:
        candidate_id = fixture.candidate()
        job_id = fixture.job(candidate_id)
        fixture.artifact(
            job_id, kind="ARCHIVE", path=fixture.work / "only-source.zip"
        )

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(fixture.service.rename_work(candidate_id, filename="x"))

        assert raised.value.code == "WORK_NOT_PACKAGED"

    def test_a_rename_onto_an_occupied_name_does_not_overwrite_it(
        self, fixture: Fixture
    ) -> None:
        """`unique_library_target` reserves this book's own path and nothing else."""
        candidate_id, _, _ = fixture.packaged()
        occupied = fixture.library / "占用.cbz"
        occupied.write_bytes(b"someone else")

        result = asyncio.run(
            fixture.service.rename_work(candidate_id, filename="占用")
        )

        assert occupied.read_bytes() == b"someone else"
        assert result["relative_path"] != "占用.cbz"


class TestSettingsAreReadPerAction:
    def test_the_roots_come_from_the_settings_service_not_from_startup(
        self, tmp_path: Path
    ) -> None:
        """An operator who corrects a directory expects the next action to use it.

        The roots are also what every path is validated against, so reading a
        stale value would validate against the wrong tree -- and then refuse to
        delete a file that is, in fact, inside the library.
        """
        fixture = Fixture(tmp_path)
        relocated = tmp_path / "relocated"
        relocated.mkdir()

        class Settings:
            async def library_path(self) -> Path:
                return relocated

            async def work_path(self) -> Path:
                return fixture.work

        service = ArchivedWorkService(
            fixture.database,
            fixture.library,
            fixture.work,
            settings_service=Settings(),
        )
        candidate_id = fixture.candidate()
        job_id = fixture.job(candidate_id)
        fixture.artifact(job_id, kind="ARCHIVE", path=fixture.work / "s.zip")
        pack_id = fixture.job(
            candidate_id,
            state=CONVERSION_STATE_FAILED,
            provider=PROVIDER_CONVERSION,
            key=f"convert:{candidate_id}",
        )
        inside_new_root = relocated / "book.cbz"
        fixture.artifact(pack_id, kind="CBZ", path=inside_new_root)

        result = asyncio.run(
            service.remove_work(candidate_id, delete_files=True)
        )

        # Validated against the *current* library, so the file is deleted rather
        # than refused as being outside the startup one.
        assert result["failed_files"] == ()
        assert not inside_new_root.exists()