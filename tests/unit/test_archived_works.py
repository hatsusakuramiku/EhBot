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


class TestExplicitArchivePath:
    """`set_archive_path`: the operator stating where one book must live.

    The distinction from `rename_work` is what it refuses, so most of these are
    refusals. A convenience rename may repair input and may suffix a taken name;
    an operator typing a path is telling the service something, and answering
    with a different path than they typed is worse than answering no.
    """

    def test_a_path_can_be_set_before_the_first_pack(
        self, fixture: Fixture
    ) -> None:
        """The whole reason the pin is keyed by candidate rather than artifact.

        「先设路径，再打包」 has to work: an operator who knows where a book
        belongs should not have to pack it to the template's guess first and move
        it afterwards. There is no CBZ here at all, so nothing moves and the pin
        is the entire effect.
        """
        candidate_id = fixture.candidate()
        job_id = fixture.job(candidate_id)
        fixture.artifact(
            job_id, kind="ARCHIVE", path=fixture.work / "source.zip"
        )

        result = asyncio.run(
            fixture.service.set_archive_path(
                candidate_id, directory="分类/作者", filename="书名"
            )
        )

        assert result["moved"] is False
        assert result["relative_path"] == "分类/作者/书名.cbz"
        pin = asyncio.run(fixture.database.archive_path_pin(candidate_id))
        assert pin["relative_path"] == "分类/作者/书名.cbz"
        assert pin["is_manual"] is True

    def test_setting_a_path_creates_the_directory_and_moves_the_file(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, cbz = fixture.packaged()

        result = asyncio.run(
            fixture.service.set_archive_path(
                candidate_id, directory="新分类/新作者", filename="新书名"
            )
        )

        assert result["moved"] is True
        assert result["created_directory"] is True
        moved = fixture.library / "新分类" / "新作者" / "新书名.cbz"
        assert moved.exists()
        assert not cbz.exists()
        # The artifact row follows the file, or the next repack would look for it
        # where it no longer is.
        work = asyncio.run(fixture.database.downloaded_work(candidate_id))
        assert work.cbz_path == str(moved)
        assert work.archive_relative_path == "新分类/新作者/新书名.cbz"

    def test_only_the_directory_can_change(self, fixture: Fixture) -> None:
        """An empty filename keeps the current one, so moving is a one-field edit."""
        candidate_id, _, _ = fixture.packaged(cbz_relative="作者/保留名.cbz")

        result = asyncio.run(
            fixture.service.set_archive_path(candidate_id, directory="别的目录")
        )

        assert result["relative_path"] == "别的目录/保留名.cbz"

    def test_an_unchanged_path_is_not_an_error(self, fixture: Fixture) -> None:
        """Refusing would read as the form being broken."""
        candidate_id, _, _ = fixture.packaged(cbz_relative="作者/示例作品.cbz")

        result = asyncio.run(
            fixture.service.set_archive_path(
                candidate_id, directory="作者", filename="示例作品"
            )
        )

        assert result["moved"] is False

    def test_a_name_another_work_pinned_is_refused_not_suffixed(
        self, fixture: Fixture
    ) -> None:
        """The requirement, stated exactly: 「已存在不允许进行调整」.

        `unique_library_target` would grow a ` (2)`, which is right when a
        *template* renders two books onto one name -- nobody chose either name, so
        the suffix is the least-bad answer. Here the operator chose, and a book
        landing at `书名 (2).cbz` is a name they did not ask for sitting beside
        one they did.
        """
        first, _, _ = fixture.packaged(cbz_relative="a/占用中.cbz")
        asyncio.run(
            fixture.service.set_archive_path(first, directory="a", filename="占用中")
        )
        second, _, _ = fixture.packaged(cbz_relative="b/其他.cbz")

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.set_archive_path(
                    second, directory="a", filename="占用中"
                )
            )

        assert raised.value.code == "PATH_TAKEN_BY_WORK"
        assert str(first) in raised.value.public_message
        # Nothing moved, so the second book is still where it was.
        assert (fixture.library / "b" / "其他.cbz").exists()

    def test_a_file_nobody_pinned_still_blocks_the_name(
        self, fixture: Fixture
    ) -> None:
        """A book packed before anyone pinned anything has no row to find it by.

        So the filesystem is the only witness that the name is taken, and
        skipping this check would have the move overwrite a book silently.
        """
        candidate_id, _, _ = fixture.packaged(cbz_relative="作者/本书.cbz")
        squatter = fixture.library / "目标" / "已存在.cbz"
        squatter.parent.mkdir(parents=True)
        squatter.write_bytes(b"another book")

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.set_archive_path(
                    candidate_id, directory="目标", filename="已存在"
                )
            )

        assert raised.value.code == "PATH_TAKEN_ON_DISK"
        assert squatter.read_bytes() == b"another book"

    @pytest.mark.parametrize(
        ("filename", "code"),
        [
            ("带有?非法字符", "SEGMENT_UNSAFE_CHARACTER"),
            ("以点结尾.", "SEGMENT_TRAILING_DOT"),
            ("con", "SEGMENT_RESERVED"),
            ("長" * 200, "SEGMENT_TOO_LONG"),
        ],
    )
    def test_an_illegal_name_is_refused_rather_than_cleaned(
        self, fixture: Fixture, filename: str, code: str
    ) -> None:
        """A repaired name is not the name the operator asked for.

        `safe_library_name` would turn each of these into something usable, which
        is correct inside a packing job -- the book is downloaded and has to land
        somewhere. It is wrong here: the operator is looking at the form, and a
        silent repair means the path they get differs from the one they typed with
        nothing on screen saying so.
        """
        candidate_id, _, cbz = fixture.packaged()

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.set_archive_path(
                    candidate_id, filename=filename
                )
            )

        assert raised.value.code == code
        assert cbz.exists()

    def test_a_directory_cannot_walk_out_of_the_library(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.set_archive_path(
                    candidate_id, directory="../../etc", filename="passwd"
                )
            )

        assert raised.value.code == "SEGMENT_TRAVERSAL"

    def test_a_refused_submission_leaves_no_directories_behind(
        self, fixture: Fixture
    ) -> None:
        """`mkdir` runs after every check, and this is what that ordering buys.

        A form the operator is iterating on would otherwise litter the library
        with empty directories from each rejected attempt.
        """
        candidate_id, _, _ = fixture.packaged()
        # An occupied name under a directory that does not exist yet. The
        # conflict is what refuses the submission, and it is checked *before*
        # anything is created -- so the new directory must not be there
        # afterwards.
        squatter = fixture.library / "目录" / "已存在.cbz"
        squatter.parent.mkdir(parents=True)
        squatter.write_bytes(b"x")

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.set_archive_path(
                    candidate_id, directory="目录", filename="已存在"
                )
            )

        assert raised.value.code == "PATH_TAKEN_ON_DISK"
        # And an illegal name never reaches `mkdir` either, which is the case a
        # form being iterated on hits repeatedly.
        with pytest.raises(ArchivedWorkError):
            asyncio.run(
                fixture.service.set_archive_path(
                    candidate_id,
                    directory="全新目录",
                    filename="非法?名字",
                )
            )
        assert not (fixture.library / "全新目录").exists()

    def test_a_pack_in_flight_refuses_the_edit(self, fixture: Fixture) -> None:
        candidate_id, _, _ = fixture.packaged(
            pack_state=CONVERSION_STATE_RUNNING
        )

        with pytest.raises(ArchivedWorkError) as raised:
            asyncio.run(
                fixture.service.set_archive_path(
                    candidate_id, filename="新名"
                )
            )

        assert raised.value.code == "WORK_PACK_RUNNING"

    def test_a_computed_pin_never_overwrites_one_the_operator_typed(
        self, fixture: Fixture
    ) -> None:
        """The `is_manual` guard, which is what protects a rename from a batch.

        A batch repack recomputes every selected work's path from the current
        template. Without this, the first batch after a rename would quietly undo
        it -- the operator's name replaced by the template's, fifty at a time.
        """
        candidate_id, _, _ = fixture.packaged()
        asyncio.run(
            fixture.service.set_archive_path(
                candidate_id, directory="手写", filename="手写名"
            )
        )

        asyncio.run(
            fixture.service.pin_computed_path(candidate_id, "模板/模板名.cbz")
        )

        pin = asyncio.run(fixture.database.archive_path_pin(candidate_id))
        assert pin["relative_path"] == "手写/手写名.cbz"
        assert pin["is_manual"] is True

    def test_a_computed_pin_is_recorded_when_nothing_was_typed(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()

        asyncio.run(
            fixture.service.pin_computed_path(candidate_id, "模板/模板名.cbz")
        )

        pin = asyncio.run(fixture.database.archive_path_pin(candidate_id))
        assert pin["relative_path"] == "模板/模板名.cbz"
        assert pin["is_manual"] is False

    def test_parking_files_the_work_under_attention_with_the_reason(
        self, fixture: Fixture
    ) -> None:
        """A skip in a flash message is gone on the next navigation.

        「这本书为什么没打包」 has to still be answerable afterwards, which is
        why the batch parks a row instead of only reporting. It is a *waiting*
        state, not a failure: nothing was attempted and the archive is intact.
        """
        candidate_id, _, _ = fixture.packaged(pack_state=CONVERSION_STATE_FAILED)

        asyncio.run(
            fixture.service.park_for_invalid_path(
                candidate_id, "SEGMENT_TOO_LONG", "文件名过长"
            )
        )

        work = asyncio.run(fixture.database.downloaded_work(candidate_id))
        assert work.pack_state == "CONVERSION_WAITING_PATH"
        assert work.pack_error_message == "文件名过长"
        # And it is in the 需干预 partition, which is where the operator looks.
        works, total = asyncio.run(
            fixture.database.list_downloaded_works(pack_filter="attention")
        )
        assert total == 1
        assert works[0].candidate_id == candidate_id

    def test_parking_leaves_a_running_pack_alone(self, fixture: Fixture) -> None:
        """The worker holds that row and would overwrite whatever we wrote."""
        candidate_id, _, _ = fixture.packaged(
            pack_state=CONVERSION_STATE_RUNNING
        )

        asyncio.run(
            fixture.service.park_for_invalid_path(
                candidate_id, "SEGMENT_TOO_LONG", "文件名过长"
            )
        )

        work = asyncio.run(fixture.database.downloaded_work(candidate_id))
        assert work.pack_state == CONVERSION_STATE_RUNNING

    def test_deleting_the_files_drops_the_pin(self, fixture: Fixture) -> None:
        """A pin naming a deleted file would hold that name against every work.

        And a later re-download would publish to a path chosen for a book that no
        longer exists. A records-only removal keeps the pin on purpose -- the file
        is still there and the decision still applies.
        """
        candidate_id, _, _ = fixture.packaged()
        asyncio.run(
            fixture.service.set_archive_path(candidate_id, filename="固定名")
        )

        asyncio.run(
            fixture.service.remove_work(candidate_id, delete_files=True)
        )

        assert asyncio.run(fixture.database.archive_path_pin(candidate_id)) is None

    def test_a_records_only_removal_keeps_the_pin(
        self, fixture: Fixture
    ) -> None:
        candidate_id, _, _ = fixture.packaged()
        asyncio.run(
            fixture.service.set_archive_path(candidate_id, filename="固定名")
        )

        asyncio.run(fixture.service.remove_work(candidate_id))

        pin = asyncio.run(fixture.database.archive_path_pin(candidate_id))
        assert pin["relative_path"] == "固定名.cbz"


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