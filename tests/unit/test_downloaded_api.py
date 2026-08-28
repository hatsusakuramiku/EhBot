"""The 已下载内容 read and batch layer (R10).

Two things are under test here and neither needs a database or a browser:

* `downloaded_snapshot` -- and in particular its `live` flag, which is the only
  thing that makes the page poll. It is computed off the *resolved* pack payload
  rather than through `is_live`, because the pack codes are derived vocabulary
  and deliberately absent from `_REGISTRIES`. That mistake is silent: the page
  renders, the badge says 打包中, and nothing ever refreshes.
* `apply_downloaded_batch` -- the partial-run and replay behaviour every batch in
  this application shares, tested through fakes for the reason
  `test_review_batch.py` uses them: a half-failed run is awkward to reach over
  HTTP and trivial to describe with a stub.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.contracts import ApiError
from app.api.downloaded import (
    MAX_DOWNLOADED_BATCH,
    apply_downloaded_batch,
    downloaded_snapshot,
)
from app.api.status import (
    DOWNLOADED_PACK_STATUS,
    DOWNLOADED_TAB_STATUS,
    downloaded_pack_view,
    downloaded_tab_view,
    is_live,
)
from app.downloads.archived import ArchivedWorkError
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_RUNNING,
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_VOLUMES,
    DOWNLOAD_STATE_COMPLETED,
    DownloadedWork,
)


def work(**overrides) -> DownloadedWork:
    values = {
        "candidate_id": 1,
        "job_id": 10,
        "provider": "TELEGRAM",
        "state": DOWNLOAD_STATE_COMPLETED,
        "title": "示例作品",
        "archive_path": "/work/source.zip",
        "archive_size": 4096,
        "updated_at": "2026-08-28T00:00:00Z",
    }
    values.update(overrides)
    return DownloadedWork(**values)


class FakeDatabase:
    """Answers the two reads the snapshot makes, recording what it was asked."""

    def __init__(self, works: list[DownloadedWork]) -> None:
        self._works = works
        self.calls: list[dict] = []

    async def list_downloaded_works(self, **kwargs):
        self.calls.append(kwargs)
        return (self._works, len(self._works))

    async def downloaded_work_counts(self) -> dict[str, int]:
        return {"all": len(self._works), "packed": 0, "unpacked": 0,
                "failed": 0, "attention": 0}


def snapshot(works: list[DownloadedWork], **kwargs) -> dict:
    return asyncio.run(downloaded_snapshot(FakeDatabase(works), **kwargs))


class TestPackVocabulary:
    def test_a_task_in_flight_outranks_an_existing_cbz(self) -> None:
        """「打包中」beats「已打包」, and the order encodes the policy.

        A book being re-packed has a CBZ on disk from its previous run. Saying
        已打包 while the packer is rewriting that file describes the previous
        result, not the current state.
        """
        view = downloaded_pack_view(
            has_cbz=True, pack_state=CONVERSION_STATE_RUNNING
        )
        assert view.code == "packing"
        assert view.live is True

    def test_a_queued_repack_is_live_so_the_page_keeps_watching(self) -> None:
        view = downloaded_pack_view(
            has_cbz=True, pack_state=CONVERSION_STATE_PENDING
        )
        assert (view.code, view.live) == ("queued", True)

    @pytest.mark.parametrize(
        "pack_state",
        [CONVERSION_STATE_WAITING_PASSWORD, CONVERSION_STATE_WAITING_VOLUMES],
    )
    def test_a_task_waiting_on_the_operator_is_not_a_failure(
        self, pack_state: str
    ) -> None:
        view = downloaded_pack_view(has_cbz=False, pack_state=pack_state)
        assert view.code == "attention"
        assert view.live is False

    def test_the_artifact_decides_once_nothing_is_running(self) -> None:
        """The same rule `work_stage` follows: the file is the only evidence.

        A COMPLETED packing task with no CBZ is not a packaged book, and a book
        with a CBZ beside a FAILED task *is* one -- the failure belongs to the
        latest attempt, and the file from the previous one still exists.
        """
        assert (
            downloaded_pack_view(
                has_cbz=True, pack_state=CONVERSION_STATE_FAILED
            ).code
            == "packed"
        )
        assert (
            downloaded_pack_view(
                has_cbz=False, pack_state=CONVERSION_STATE_COMPLETED
            ).code
            == "unpacked"
        )
        assert (
            downloaded_pack_view(has_cbz=False, pack_state=None).code
            == "unpacked"
        )
        assert (
            downloaded_pack_view(
                has_cbz=False, pack_state=CONVERSION_STATE_FAILED
            ).code
            == "failed"
        )

    def test_an_unknown_tab_raises_rather_than_rendering_the_typo(self) -> None:
        """`/downloaded?tab=nonsense` must 404.

        Unlike `candidate_tab_view`, which describes a value its route already
        validated, this one reads a query string an operator can type. The same
        rule `settings_section_view` follows.
        """
        with pytest.raises(KeyError):
            downloaded_tab_view("nonsense")
        assert downloaded_tab_view("all").code == "all"

    def test_the_derived_codes_are_absent_from_the_generic_registry(
        self,
    ) -> None:
        """This is the trap `downloaded_snapshot` has to route around.

        The pack codes are not job states, so `is_live` cannot see them. Anything
        deciding「要继续轮询吗」through `is_live("packing")` gets False and the
        page freezes on 打包中 forever. Asserting it here means the workaround in
        the snapshot has a reason attached rather than looking redundant.
        """
        for code, view in DOWNLOADED_PACK_STATUS.items():
            assert is_live(code) is False, code
            if view.live:
                assert view.to_payload()["live"] is True


class TestSnapshot:
    def test_the_page_polls_only_while_something_is_packing(self) -> None:
        idle = snapshot([work(cbz_path="/library/a.cbz")])
        assert idle["live"] is False

        busy = snapshot(
            [
                work(cbz_path="/library/a.cbz"),
                work(candidate_id=2, pack_state=CONVERSION_STATE_RUNNING),
            ]
        )
        assert busy["live"] is True

    def test_the_tab_travels_as_resolved_vocabulary(self) -> None:
        payload = snapshot([], tab="failed")
        assert payload["tab"] == DOWNLOADED_TAB_STATUS["failed"].to_payload()

    def test_the_query_is_passed_through_untranslated(self) -> None:
        database = FakeDatabase([])
        asyncio.run(
            downloaded_snapshot(
                database,
                tab="packed",
                search="  汉化  ",
                sort="largest",
                offset=50,
                limit=25,
            )
        )
        assert database.calls == [
            {
                "search": "  汉化  ",
                "pack_filter": "packed",
                "sort": "largest",
                "offset": 50,
                "limit": 25,
            }
        ]

    def test_every_work_carries_its_own_actions(self) -> None:
        payload = snapshot(
            [
                work(cbz_path="/library/a.cbz"),
                work(candidate_id=2, pack_state=CONVERSION_STATE_RUNNING),
                work(candidate_id=3, state="PENDING"),
            ]
        )
        packaged, packing, in_flight = payload["works"]

        assert packaged["actions"] == {
            "repack": True,
            "remove": True,
            "redownload": True,
            "rename": True,
        }
        # Nothing may touch a work the packer holds.
        assert packing["actions"] == {
            "repack": False,
            "remove": False,
            "redownload": False,
            "rename": False,
        }
        # A download in flight blocks removal and re-download; packing an
        # earlier archive is still fine, and there is no CBZ to rename.
        assert in_flight["actions"] == {
            "repack": True,
            "remove": False,
            "redownload": False,
            "rename": False,
        }

    def test_a_work_links_to_the_one_detail_page(self) -> None:
        (item,) = snapshot([work(candidate_id=42)])["works"]
        assert item["href"] == "/works/42"


class FakeArchived:
    """Refuses what it has already removed, the way the real service does."""

    def __init__(self) -> None:
        self.removed: list[tuple[int, bool]] = []
        self.redownloaded: list[tuple[int, bool]] = []

    async def remove_work(self, candidate_id, *, delete_files=False,
                          operator_name="admin"):
        if any(entry[0] == candidate_id for entry in self.removed):
            raise ArchivedWorkError(
                "WORK_NOT_DOWNLOADED", f"作品 #{candidate_id} 没有已下载的档案"
            )
        self.removed.append((candidate_id, bool(delete_files)))
        return {"candidate_id": candidate_id, "removed_jobs": 1}

    async def redownload_work(self, candidate_id, *, repack=False):
        self.redownloaded.append((candidate_id, bool(repack)))
        return {"candidate_id": candidate_id, "repack": bool(repack)}


class FakeConversion:
    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue_for_candidate(self, candidate_id: int) -> int:
        self.enqueued.append(candidate_id)
        return candidate_id * 100


def run_batch(archived, conversion, action, ids, **kwargs) -> dict:
    return asyncio.run(
        apply_downloaded_batch(archived, conversion, action, ids, **kwargs)
    )


class TestBatch:
    def test_an_unknown_action_is_refused_before_anything_is_touched(
        self,
    ) -> None:
        """The check is inside the coroutine because two callers share it.

        The no-JS form posts a raw field, so a batch reaching the services with
        whatever the browser sent is what this guards.
        """
        archived, conversion = FakeArchived(), FakeConversion()
        with pytest.raises(ApiError) as raised:
            run_batch(archived, conversion, "delete", [1, 2])

        assert raised.value.code == "ACTION_UNKNOWN"
        assert raised.value.details == {
            "allowed": ["redownload", "remove", "repack"]
        }
        assert archived.removed == []
        assert conversion.enqueued == []

    def test_repack_goes_through_the_one_packing_entry_point(self) -> None:
        conversion = FakeConversion()
        result = run_batch(FakeArchived(), conversion, "repack", [7, 8])

        assert conversion.enqueued == [7, 8]
        assert [entry["candidate_id"] for entry in result["applied"]] == [7, 8]
        assert result["skipped"] == []

    def test_deleting_files_is_opt_in_and_echoed_back(self) -> None:
        """The flag has to survive the round trip.

        A client cannot otherwise tell a records-only removal from one that took
        the files, and「默认不移除」is only meaningful if the default is visible.
        """
        archived = FakeArchived()
        records_only = run_batch(archived, FakeConversion(), "remove", [1])
        assert archived.removed == [(1, False)]
        assert records_only["delete_files"] is False

        archived = FakeArchived()
        with_files = run_batch(
            archived, FakeConversion(), "remove", [1], delete_files=True
        )
        assert archived.removed == [(1, True)]
        assert with_files["delete_files"] is True

    def test_the_flag_is_never_echoed_for_an_action_that_ignores_it(
        self,
    ) -> None:
        result = run_batch(
            FakeArchived(), FakeConversion(), "repack", [1], delete_files=True
        )
        assert result["delete_files"] is False

    def test_one_refusal_does_not_refuse_the_rest(self) -> None:
        archived = FakeArchived()
        asyncio.run(archived.remove_work(2))

        result = run_batch(archived, FakeConversion(), "remove", [1, 2, 3])

        assert [entry["candidate_id"] for entry in result["applied"]] == [1, 3]
        assert result["skipped"] == [
            {
                "candidate_id": 2,
                "code": "WORK_NOT_DOWNLOADED",
                "message": "作品 #2 没有已下载的档案",
            }
        ]
        assert result["requested"] == 3

    def test_a_replayed_batch_changes_nothing_the_first_one_did(self) -> None:
        """The double-clicked 「移除记录」.

        Every work in the second send is already gone, so all of them are
        skipped and nothing is removed twice.
        """
        archived, conversion = FakeArchived(), FakeConversion()
        first = run_batch(archived, conversion, "remove", [1, 2])
        second = run_batch(archived, conversion, "remove", [1, 2])

        assert len(first["applied"]) == 2
        assert second["applied"] == []
        assert len(second["skipped"]) == 2
        assert archived.removed == [(1, False), (2, False)]

    def test_an_unexpected_error_is_not_reported_as_a_skip(self) -> None:
        """A broken filesystem must not read as「1 件跳过」."""

        class Broken(FakeArchived):
            async def remove_work(self, candidate_id, **kwargs):
                raise OSError("disk gone")

        with pytest.raises(OSError):
            run_batch(Broken(), FakeConversion(), "remove", [1])

    def test_each_applied_work_is_announced_once(self) -> None:
        announced: list[int] = []
        archived = FakeArchived()
        asyncio.run(archived.remove_work(2))

        run_batch(
            archived,
            FakeConversion(),
            "remove",
            [1, 2, 3],
            announce=announced.append,
        )

        # A skipped work is not announced: nothing about it changed, and the
        # browser would re-read state for a row that is still where it was.
        assert announced == [1, 3]

    def test_redownload_carries_the_repack_choice_per_work(self) -> None:
        archived = FakeArchived()
        run_batch(
            archived, FakeConversion(), "redownload", [4, 5], repack=True
        )
        assert archived.redownloaded == [(4, True), (5, True)]

    def test_an_empty_selection_does_nothing_rather_than_failing(self) -> None:
        result = run_batch(FakeArchived(), FakeConversion(), "repack", [])
        assert result == {
            "action": "repack",
            "requested": 0,
            "applied": [],
            "skipped": [],
            "delete_files": False,
        }


class TestSelectionValidation:
    """`_work_ids` guards the JSON endpoint; the form path dedups for itself."""

    def test_a_selection_is_deduplicated_in_the_order_it_arrived(self) -> None:
        from app.api.downloaded import _work_ids

        assert _work_ids([3, 1, 3, 2, 1]) == [3, 1, 2]

    def test_an_oversized_selection_is_refused_rather_than_truncated(
        self,
    ) -> None:
        """Acting on the first hundred of what was selected is the worse answer."""
        from app.api.downloaded import _work_ids

        with pytest.raises(ApiError) as raised:
            _work_ids(list(range(MAX_DOWNLOADED_BATCH + 1)))
        assert raised.value.code == "BATCH_TOO_LARGE"
        assert raised.value.details == {"limit": MAX_DOWNLOADED_BATCH}

    @pytest.mark.parametrize("raw", [[], None, "1,2", {"a": 1}])
    def test_a_missing_selection_names_itself(self, raw) -> None:
        from app.api.downloaded import _work_ids

        with pytest.raises(ApiError) as raised:
            _work_ids(raw)
        assert raised.value.code == "WORK_IDS_REQUIRED"

    def test_a_non_numeric_id_is_refused(self) -> None:
        from app.api.downloaded import _work_ids

        with pytest.raises(ApiError) as raised:
            _work_ids(["abc"])
        assert raised.value.code == "WORK_ID_INVALID"