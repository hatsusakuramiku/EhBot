"""Pure downloaded vocabulary, snapshot and selection validation.

Transactional batch/path regressions live in test_downloaded_batches.py and
test_downloaded_commands.py against a real temporary database.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.contracts import ApiError
from app.api.downloaded import (
    MAX_DOWNLOADED_BATCH,
    downloaded_snapshot,
)
from app.api.status import (
    DOWNLOADED_PACK_STATUS,
    DOWNLOADED_TAB_STATUS,
    downloaded_pack_view,
    downloaded_tab_view,
    is_live,
)
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

    def test_the_queued_badge_does_not_borrow_a_tab_word(self) -> None:
        """R34: 「待打包」 named both the tab and the queued badge.

        The tab asks「开始打包了吗」(nothing queued yet) while the badge says
        「有任务在排队」. Sharing one word meant a book the operator had just
        queued wore a badge naming a list it could not be in -- 「界面都是待打包，
        但待打包列表里没有」. The tab and the `unpacked` badge now use the same
        word for the same fact, and the queued badge uses its own.
        """
        tab_words = {view.label for view in DOWNLOADED_TAB_STATUS.values()}
        assert DOWNLOADED_PACK_STATUS["queued"].label not in tab_words
        assert (
            DOWNLOADED_TAB_STATUS["unpacked"].label
            == DOWNLOADED_PACK_STATUS["unpacked"].label
        )

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

    def test_the_tab_travels_as_a_key_beside_its_words(self) -> None:
        """`tab` is the identifier; `tab_view` carries the vocabulary.

        They were one field until R34, and that field held the resolved view.
        Because the page spreads this payload into its own context, every
        consumer of 「which tab is this」 -- the poller's `data-tab`, the hidden
        `tab` fields the batch forms post back, the tab strip's current key --
        received the repr of a dict, and each poll became
        `/api/v1/downloaded?tab={'code': ...}` and a 400. Asserting the shape
        here is what keeps the identifier a string.
        """
        payload = snapshot([], tab="failed")
        assert payload["tab"] == "failed"
        assert payload["tab_view"] == DOWNLOADED_TAB_STATUS["failed"].to_payload()

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
                # R61: the search box is parsed in the database layer now, so
                # the word travels alone; the direction rides beside the sort.
                "pack_filter": "packed",
                "sort": "largest",
                "direction": None,
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
