"""The AI path decision: payload, prompt, fingerprint, cleaning, cache.

Two layers under test, and they fail differently:

* the **pure** functions -- the metadata payload, the message shape, the
  fingerprint and `clean_ai_path`. No database and no model; the assertions are
  about the exact string that would become a folder name, which is the part of
  this feature an operator sees.
* the **service** -- `AiPathService` and the two `ConversionService` call sites.
  The model is a fake object, because what is being tested is 「缓存命中了吗、
  失败写没写库、渲染会不会偷偷发请求」 rather than HTTP.

The invariant worth naming: **a render never asks the model.** `current`,
`is_current` and `badge` read one row; every test that asserts a cache hit also
asserts the fake was not called, which is what keeps 「打开一次详情页就向第三方发出
N 条元数据」 from coming back.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

from app.ai.client import AI_BAD_RESPONSE
from app.ai.errors import (
    AI_PATH_INVALID,
    AI_PATH_MISSING,
    AI_PATH_UNAVAILABLE,
    AiError,
)
from app.ai.models import (
    MAX_TAGS_IN_PROMPT,
    AiAnswer,
    AiModelChainEntry,
    AiProvider,
    AiProviderModel,
    AiRequestParams,
)
from app.ai.paths import (
    AiPathService,
    answer_path,
    chain_signature,
    clean_ai_path,
    fingerprint_of,
    prompt_hash_of,
)
from app.ai.prompt import (
    DEFAULT_AI_PROMPT,
    build_messages,
    build_metadata_payload,
    fields_from_metadata,
    metadata_payload,
    split_values,
)
from app.archive.service import (
    PATH_SOURCE_AI,
    ArchiveSettingsService,
)
from app.conversion.convert import ConversionError
from app.conversion.naming import LibraryLimits, LibraryPathError

#: Small enough to cross by hand; the real ceiling is the filesystem's.
SMALL = LibraryLimits(name_max=16, relative_max=40)
from app.conversion.service import ConversionService
from app.db.database import Database
from app.downloads.archived import ArchivedWorkError

KEY = '{"directory": "同人志/作者", "filename": "作品"}'


def _rows(**fields: object) -> list[SimpleNamespace]:
    """Metadata rows the way `_fetch_metadata_sync` hands them over."""
    return [
        SimpleNamespace(field_name=name, field_value=value)
        for name, value in fields.items()
    ]


def _chain(count: int = 2, *, base_url: str = "http://local/v1", name: str = "本地"):
    provider = AiProvider(
        provider_id=1, name=name, code="openai", base_url=base_url
    )
    return tuple(
        AiModelChainEntry(
            position=position,
            provider=provider,
            model=AiProviderModel(
                model_id=100 + position, provider_id=1, name=f"m{position}"
            ),
        )
        for position in range(count)
    )


class _FakeAi:
    """A stand-in for `AiProviderService`: a scripted answer, and a call log."""

    def __init__(
        self,
        text: str = KEY,
        *,
        failure: AiError | None = None,
        chain: Sequence[AiModelChainEntry] | None = None,
    ) -> None:
        self.text = text
        self.failure = failure
        self.calls: list[list[dict[str, str]]] = []
        self._chain = tuple(chain) if chain is not None else _chain()
        self.provider_name = "本地"
        self.provider_id = 1
        self.model_name = self._chain[0].model.name if self._chain else "m0"

    async def chain(self, scope: str = "default") -> tuple[AiModelChainEntry, ...]:
        return self._chain if scope == "default" else ()

    async def effective_chain(
        self, scope: str = "default"
    ) -> tuple[AiModelChainEntry, ...]:
        return self._chain

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        validate=None,
        stream: bool | None = None,
    ) -> AiAnswer:
        self.calls.append([dict(message) for message in messages])
        if self.failure is not None:
            raise self.failure
        if validate is not None:
            validate(self.text)
        return AiAnswer(
            text=self.text,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            model_name=self.model_name,
            key_id=1,
        )


async def _candidate(database: Database, candidate_id: int = 7) -> int:
    """A candidate row, because `ai_path_suggestions` has a foreign key to it.

    Inserted directly rather than through the ingest path: these tests are about
    the answer cache, and a real candidate would drag the review pipeline in for
    no assertion.
    """

    def _write() -> None:
        with database._connect() as connection:
            connection.execute(
                "INSERT INTO candidates (id, status, created_at, updated_at) "
                "VALUES (?, 'APPROVED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (candidate_id,),
            )

    import asyncio as _asyncio

    await _asyncio.to_thread(_write)
    return candidate_id


async def _seed(tmp_path: Path) -> tuple[Database, ArchiveSettingsService]:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    settings = ArchiveSettingsService(
        database,
        tmp_path / "work",
        default_library_path=tmp_path / "library",
        default_work_path=tmp_path / "work",
    )
    return database, settings


def _conversion(
    database: Database,
    settings: ArchiveSettingsService,
    tmp_path: Path,
    ai: object | None,
    *,
    refile: object | None = None,
) -> ConversionService:
    return ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        settings_service=settings,
        data_path=tmp_path / "data",
        ai_service=ai,
        refile=refile,
    )


async def _job_details(database: Database, job_id: int) -> dict:
    """The `details_json` of one job, for asserting what a terminal write said."""
    def _read() -> dict:
        with database._connect() as connection:
            row = connection.execute(
                "SELECT details_json FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(str(row[0])) if row and row[0] else {}

    return await asyncio.to_thread(_read)


async def _seed_pending_job(database: Database, details: dict) -> int:
    """A `CONVERSION_PENDING` row carrying `details`, with nothing else.

    `_claim_pending_job_sync` is what is under test, and it reads the job row
    only -- so the readiness gate that a real enqueue passes through is not part
    of the question here.
    """
    def _write() -> int:
        with database._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (7, 'CONVERSION', 'CONVERSION_PENDING', 100, 0, "
                "?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (f"convert:7:{json.dumps(details)}", json.dumps(details)),
            )
            return int(cursor.lastrowid)

    return await asyncio.to_thread(_write)


# ---------------------------------------------------------------------------
#  The payload
# ---------------------------------------------------------------------------


class TestMetadataPayload:
    def test_fields_take_the_first_row_for_each_name(self) -> None:
        # `field_name` repeats, so a kwargs helper cannot express this.
        rows = [
            SimpleNamespace(field_name="Title", field_value="英文"),
            SimpleNamespace(field_name="Title", field_value="另一个英文"),
        ]
        assert fields_from_metadata(rows) == {"Title": "英文"}

    def test_arrays_come_from_the_raw_upstream_value(self) -> None:
        """`Artist` may already be translated; the prompt asks for 原文照抄."""
        payload = build_metadata_payload(
            {
                "ArtistRaw": "作者A, 作者B",
                "Artist": "译者A",
                "Group": "G-Power!",
            }
        )
        assert payload["artists"] == ["作者A", "作者B"]
        assert payload["groups"] == ["G-Power!"]
        # Missing entirely is an empty list, not a null: the model's judgement
        # 「几个人合作」 reads counts, and `null` would invite an invention.
        assert payload["parody"] == []

    def test_the_tag_list_is_capped(self) -> None:
        payload = build_metadata_payload(
            {"TagsRaw": ", ".join(f"tag{index}" for index in range(200))}
        )
        assert len(payload["tags"]) == MAX_TAGS_IN_PROMPT

    def test_page_count_is_read_from_a_noisy_value(self) -> None:
        assert build_metadata_payload({"Pages": "42 頁"})["page_count"] == 42
        assert build_metadata_payload({})["page_count"] is None

    def test_split_values_keeps_order_and_drops_duplicates(self) -> None:
        assert split_values("a, b, a\nc") == ["a", "b", "c"]

    def test_metadata_payload_accepts_the_row_list(self) -> None:
        payload = metadata_payload(_rows(JapaneseTitle="題", CategoryRaw="Doujinshi"))
        assert payload["japanese_title"] == "題"
        assert payload["category_raw"] == "Doujinshi"

    def test_no_path_or_candidate_detail_ever_leaves(self) -> None:
        """The model is given the book, not the deployment."""
        payload = build_metadata_payload({"Title": "t"})
        assert set(payload) == {
            "japanese_title",
            "english_title",
            "artists",
            "groups",
            "parody",
            "category",
            "category_raw",
            "language",
            "tags",
            "page_count",
        }


class TestMessages:
    def test_no_placeholder_appends_the_payload_as_a_user_message(self) -> None:
        messages = build_messages("去决定路径", {"filename": "x"})
        assert messages[0] == {"role": "system", "content": "去决定路径"}
        assert messages[1]["role"] == "user"
        assert json.loads(messages[1]["content"]) == {"filename": "x"}

    def test_a_placeholder_is_replaced_in_place(self) -> None:
        messages = build_messages("元数据：{{metadata}}。", {"a": 1})
        assert len(messages) == 1
        assert "{{metadata}}" not in messages[0]["content"]
        assert '{"a": 1}' in messages[0]["content"]

    def test_the_default_prompt_uses_the_appended_shape(self) -> None:
        assert "{{metadata}}" not in DEFAULT_AI_PROMPT
        assert len(build_messages(DEFAULT_AI_PROMPT, {"a": 1})) == 2


# ---------------------------------------------------------------------------
#  Cleaning and refusal
# ---------------------------------------------------------------------------


class TestCleanAiPath:
    def test_illegal_characters_are_repaired_not_refused(self) -> None:
        relative, sanitised = clean_ai_path("同人志/社團: A//作者", "作品:1/2")
        assert relative.as_posix() == "同人志/社團 A/作者/作品 1 2.cbz"
        assert sanitised == ["社團: A", "作品:1/2"]

    def test_an_empty_directory_is_a_flat_path(self) -> None:
        relative, sanitised = clean_ai_path("", "作品")
        assert relative.as_posix() == "作品.cbz"
        assert sanitised == []

    def test_a_level_that_sanitises_away_is_omitted(self) -> None:
        """Not fabricated and not an error: it is the missing-社团 case."""
        relative, _ = clean_ai_path("同人志/.../作者", "作品")
        assert relative.as_posix() == "同人志/作者/作品.cbz"

    def test_the_cbz_suffix_is_appended_once(self) -> None:
        relative, _ = clean_ai_path("a", "作品.cbz")
        assert relative.as_posix() == "a/作品.cbz.cbz"

    def test_backslashes_are_treated_as_separators(self) -> None:
        relative, _ = clean_ai_path("a\\b", "c")
        assert relative.as_posix() == "a/b/c.cbz"

    @pytest.mark.parametrize(
        "directory, filename",
        [
            ("a/../b", "c"),
            ("..", "c"),
            ("/etc", "c"),
            ("C:/windows", "c"),
            ("a", ""),
            ("a", ".."),
            ("a", "..."),
        ],
    )
    def test_a_traversal_or_an_empty_name_is_refused(
        self, directory: str, filename: str
    ) -> None:
        with pytest.raises(AiError) as caught:
            clean_ai_path(directory, filename)
        assert caught.value.code == AI_PATH_INVALID

    def test_a_path_past_the_ceiling_is_refused(self) -> None:
        # Each level is truncated to the component ceiling, so it takes a few to
        # pass the whole-path one -- and 「整条太长」 is a refusal rather than
        # another truncation, because a path shortened at two levels is a name
        # nobody chose.
        with pytest.raises(AiError) as caught:
            clean_ai_path("/".join(["x" * 12] * 4), "y" * 40, limits=SMALL)
        assert caught.value.code == AI_PATH_INVALID


class TestAnswerPath:
    def test_an_object_wrapped_in_prose_is_read(self) -> None:
        relative, _ = answer_path(f"好的：{KEY} 希望有帮助。")
        assert relative.as_posix() == "同人志/作者/作品.cbz"

    def test_a_fenced_object_is_read(self) -> None:
        relative, _ = answer_path(f"```json\n{KEY}\n```")
        assert relative.as_posix() == "同人志/作者/作品.cbz"

    def test_prose_without_an_object_keeps_the_parser_code(self) -> None:
        with pytest.raises(AiError) as caught:
            answer_path("抱歉，我不能这么做。")
        assert caught.value.code == AI_BAD_RESPONSE

    def test_a_non_string_field_is_refused(self) -> None:
        with pytest.raises(AiError) as caught:
            answer_path('{"directory": 3, "filename": "c"}')
        assert caught.value.code == AI_PATH_INVALID


# ---------------------------------------------------------------------------
#  Fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def _print(self, *, payload=None, prompt="p", signature="s") -> str:
        return fingerprint_of(payload or {"a": 1}, prompt, signature)

    def test_the_same_inputs_hash_the_same(self) -> None:
        assert self._print() == self._print()

    def test_metadata_prompt_and_chain_each_change_it(self) -> None:
        base = self._print()
        assert self._print(payload={"a": 2}) != base
        assert self._print(prompt="q") != base
        assert self._print(signature="t") != base

    def test_key_order_does_not_change_it(self) -> None:
        assert fingerprint_of({"a": 1, "b": 2}, "p", "s") == fingerprint_of(
            {"b": 2, "a": 1}, "p", "s"
        )

    def test_the_chain_signature_keeps_the_order(self) -> None:
        first = _chain(2)
        second = (first[1], first[0])
        assert chain_signature(first) != chain_signature(second)

    def test_renaming_a_provider_does_not_change_it(self) -> None:
        """Cosmetic edits must not invalidate the library; a changed address must."""
        assert chain_signature(_chain(1, name="甲")) == chain_signature(
            _chain(1, name="乙")
        )
        assert chain_signature(_chain(1, base_url="http://a")) != chain_signature(
            _chain(1, base_url="http://b")
        )

    def test_request_params_are_part_of_the_signature(self) -> None:
        """The body decides the answer, so a tuned knob must invalidate the cache.

        R31 made `{model, messages}` the only guaranteed fields and let every
        other parameter be opted into per model; a signature that ignored them
        would keep calling a cached path 「current」 after the request changed.
        """
        base = _chain(1)
        warmer = (
            replace(
                base[0],
                model=replace(
                    base[0].model,
                    params=AiRequestParams(temperature=0.9),
                ),
            ),
        )
        assert chain_signature(base) != chain_signature(warmer)

    def test_param_key_order_does_not_change_the_signature(self) -> None:
        """`extra_body` is an object an operator types; its spelling is not identity."""
        first = _chain(1)[0]
        second = _chain(1)[0]
        first = replace(
            first,
            model=replace(
                first.model,
                params=AiRequestParams(extra_body={"a": 1, "b": 2}),
            ),
        )
        second = replace(
            second,
            model=replace(
                second.model,
                params=AiRequestParams(extra_body={"b": 2, "a": 1}),
            ),
        )
        assert chain_signature((first,)) == chain_signature((second,))

    def test_the_prompt_hash_tracks_only_the_prompt(self) -> None:
        assert prompt_hash_of("a") == prompt_hash_of("a")
        assert prompt_hash_of("a") != prompt_hash_of("b")


# ---------------------------------------------------------------------------
#  The service
# ---------------------------------------------------------------------------


class TestAiPathService:
    @pytest.mark.asyncio
    async def test_a_first_decision_calls_the_model_and_caches_it(
        self, tmp_path: Path, caplog
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi()
        service = AiPathService(database, ai, settings)
        metadata = _rows(Title="作品")

        import logging

        with caplog.at_level(logging.INFO, logger="app.ai.paths"):
            outcome = await service.resolve(7, metadata)
        assert outcome.from_cache is False
        assert outcome.suggestion.relative_path == "同人志/作者/作品.cbz"
        assert outcome.suggestion.directory == "同人志/作者"
        assert outcome.suggestion.filename == "作品"
        assert len(ai.calls) == 1
        stored = await database.get_ai_path_suggestion(7)
        assert stored is not None and stored.relative_path == "同人志/作者/作品.cbz"
        assert "ai_path_suggested" in [r.message for r in caplog.records]

    @pytest.mark.asyncio
    async def test_a_current_cache_is_returned_without_calling_out(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi()
        service = AiPathService(database, ai, settings)
        metadata = _rows(Title="作品")
        await service.resolve(7, metadata)

        outcome = await service.resolve(7, metadata)
        assert outcome.from_cache is True
        assert len(ai.calls) == 1

    @pytest.mark.asyncio
    async def test_a_changed_prompt_invalidates_the_cache(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi()
        service = AiPathService(database, ai, settings)
        metadata = _rows(Title="作品")
        await service.resolve(7, metadata)

        await settings.save_ai_prompt("新的提示词")
        outcome = await service.resolve(7, metadata)
        assert outcome.from_cache is False
        assert len(ai.calls) == 2

    @pytest.mark.asyncio
    async def test_a_failure_is_not_written_down(
        self, tmp_path: Path, caplog
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi(failure=AiError(AI_PATH_UNAVAILABLE, "全挂了"))
        service = AiPathService(database, ai, settings)

        import logging

        with caplog.at_level(logging.WARNING, logger="app.ai.paths"):
            with pytest.raises(AiError):
                await service.resolve(7, _rows(Title="作品"))
        assert await database.get_ai_path_suggestion(7) is None
        assert "ai_path_failed" in [r.message for r in caplog.records]

    @pytest.mark.asyncio
    async def test_a_repaired_answer_is_logged(self, tmp_path: Path, caplog) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi('{"directory": "a/b:c", "filename": "d?e"}')
        service = AiPathService(database, ai, settings)

        import logging

        with caplog.at_level(logging.INFO, logger="app.ai.paths"):
            await service.resolve(7, _rows(Title="作品"))
        records = [r for r in caplog.records if r.message == "ai_path_sanitized"]
        assert records and records[0].segments == ["b:c", "d?e"]

    @pytest.mark.asyncio
    async def test_current_hides_a_stale_row_but_badge_still_labels_it(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi()
        service = AiPathService(database, ai, settings)
        metadata = _rows(Title="作品")
        await service.resolve(7, metadata)

        await settings.save_ai_prompt("改过的提示词")
        assert await service.current(7, metadata) is None
        # 「这本书当时是模型命名的」 remains true after a prompt change.
        assert await service.badge(7, "同人志/作者/作品.cbz") is True

    @pytest.mark.asyncio
    async def test_a_rename_turns_the_badge_off(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        service = AiPathService(database, _FakeAi(), settings)
        await service.resolve(7, _rows(Title="作品"))
        assert await service.badge(7, "别的/名字.cbz") is False
        assert await service.badge(7, None) is False

    @pytest.mark.asyncio
    async def test_the_predicate_needs_the_recorded_path_to_match(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        service = AiPathService(database, _FakeAi(), settings)
        metadata = _rows(Title="作品")
        await service.resolve(7, metadata)
        assert await service.is_current(7, metadata, "同人志/作者/作品.cbz")
        assert not await service.is_current(7, metadata, "别的.cbz")
        assert not await service.is_current(7, metadata, None)


# ---------------------------------------------------------------------------
#  ConversionService: the two call sites
# ---------------------------------------------------------------------------


class TestConversionServiceAiPaths:
    @pytest.mark.asyncio
    async def test_a_pack_in_ai_mode_lands_on_the_model_path(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi()
        conversion = _conversion(database, settings, tmp_path, ai)
        await settings.save_path_source(PATH_SOURCE_AI)

        target = await conversion._library_target(
            7, tmp_path / "library", _rows(Title="作品"), "作品"
        )
        assert target.as_posix().endswith("library/同人志/作者/作品.cbz")
        assert len(ai.calls) == 1

    @pytest.mark.asyncio
    async def test_a_manual_pin_beats_the_model(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)

        def _pin() -> None:
            with database._connect() as connection:
                connection.execute(
                    "INSERT INTO candidates (id, status, created_at, updated_at) "
                    "VALUES (7, 'APPROVED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO work_archive_paths "
                    "(candidate_id, relative_path, is_manual, operator_name) "
                    "VALUES (7, '人工/名字.cbz', 1, 'admin')"
                )

        import asyncio as _asyncio

        await _asyncio.to_thread(_pin)
        ai = _FakeAi()
        conversion = _conversion(database, settings, tmp_path, ai)
        await settings.save_path_source(PATH_SOURCE_AI)

        target = await conversion._library_target(
            7, tmp_path / "library", _rows(Title="作品"), "作品"
        )
        assert target.as_posix().endswith("library/人工/名字.cbz")
        assert ai.calls == []

    @pytest.mark.asyncio
    async def test_a_failure_without_the_fallback_switch_is_a_refusal(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        ai = _FakeAi(failure=AiError(AI_PATH_UNAVAILABLE, "所有 AI 模型都不可用"))
        conversion = _conversion(database, settings, tmp_path, ai)
        await settings.save_path_source(PATH_SOURCE_AI)

        with pytest.raises(LibraryPathError) as caught:
            await conversion._library_target(
                7, tmp_path / "library", _rows(Title="作品"), "作品"
            )
        assert caught.value.code == AI_PATH_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_the_fallback_switch_uses_the_template(
        self, tmp_path: Path, caplog
    ) -> None:
        database, settings = await _seed(tmp_path)
        ai = _FakeAi(failure=AiError(AI_PATH_UNAVAILABLE, "所有 AI 模型都不可用"))
        conversion = _conversion(database, settings, tmp_path, ai)
        await settings.save_path_source(PATH_SOURCE_AI)
        await settings.save_ai_fallback_to_rules(True)

        import logging

        with caplog.at_level(logging.INFO, logger="app.conversion.service"):
            target = await conversion._library_target(
                7, tmp_path / "library", _rows(Title="作品"), "作品"
            )
        assert target.as_posix().endswith("library/作品.cbz")
        assert "ai_path_fallback" in [r.message for r in caplog.records]

    @pytest.mark.asyncio
    async def test_a_missing_ai_service_is_a_refusal_not_a_template_path(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        conversion = _conversion(database, settings, tmp_path, None)
        await settings.save_path_source(PATH_SOURCE_AI)

        with pytest.raises(LibraryPathError) as caught:
            await conversion._library_target(
                7, tmp_path / "library", _rows(Title="作品"), "作品"
            )
        assert caught.value.code == AI_PATH_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_the_operator_side_never_asks_the_model(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        ai = _FakeAi()
        conversion = _conversion(database, settings, tmp_path, ai)
        await settings.save_path_source(PATH_SOURCE_AI)

        with pytest.raises(LibraryPathError) as caught:
            await conversion.planned_library_path(7, "作品", _rows(Title="作品"))
        assert caught.value.code == AI_PATH_MISSING
        assert ai.calls == []

    @pytest.mark.asyncio
    async def test_the_operator_side_reads_a_current_cache(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        ai = _FakeAi()
        conversion = _conversion(database, settings, tmp_path, ai)
        await settings.save_path_source(PATH_SOURCE_AI)
        await AiPathService(database, ai, settings).resolve(7, _rows(Title="作品"))

        planned = await conversion.planned_library_path(
            7, "作品", _rows(Title="作品")
        )
        assert planned.as_posix() == "同人志/作者/作品.cbz"
        assert len(ai.calls) == 1

    @pytest.mark.asyncio
    async def test_template_mode_never_touches_the_ai_layer(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        ai = _FakeAi()
        conversion = _conversion(database, settings, tmp_path, ai)

        planned = await conversion.planned_library_path(
            7, "作品", _rows(Title="作品")
        )
        assert planned.as_posix() == "作品.cbz"
        assert ai.calls == []
        assert await conversion.current_ai_path(7, _rows(Title="作品")) is None

async def _seed_packed(database: Database, tmp_path: Path, candidate_id: int = 7) -> int:
    """A candidate with a published CBZ and its (completed) packing job.

    The job row is the `convert:<id>` row the whole conversion surface keys on,
    and its CBZ artifact is what makes the book 「已打包」. Seeded directly for
    the reason `_candidate` is: the refile job's question is 「它去哪」, not how
    the book got here.
    """
    await _candidate(database, candidate_id)
    cbz = tmp_path / "library" / "旧" / "书.cbz"
    cbz.parent.mkdir(parents=True, exist_ok=True)
    cbz.write_bytes(b"cbz")

    def _write() -> int:
        with database._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (?, 'CONVERSION', 'CONVERSION_COMPLETED', 100, 0, "
                "?, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (candidate_id, f"convert:{candidate_id}"),
            )
            job_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, library_relative_path, "
                " created_at) VALUES (?, 'CBZ', ?, ?, CURRENT_TIMESTAMP)",
                (job_id, str(cbz), "旧/书.cbz"),
            )
            connection.execute(
                "INSERT INTO work_archive_paths "
                "(candidate_id, relative_path, is_manual, operator_name) "
                "VALUES (?, '旧/书.cbz', 0, 'admin')",
                (candidate_id,),
            )
            return job_id

    import asyncio as _asyncio

    return await _asyncio.to_thread(_write)


class _FakeRefile:
    """The move the refile job hands to the archive service."""

    def __init__(self, *, failure: Exception | None = None, moved: bool = True):
        self.failure = failure
        self.moved = moved
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, candidate_id: int, relative_path: str) -> dict:
        self.calls.append((candidate_id, relative_path))
        if self.failure is not None:
            raise self.failure
        return {
            "candidate_id": candidate_id,
            "path": f"/library/{relative_path}",
            "relative_path": relative_path,
            "moved": self.moved,
        }


class TestRefileJob:
    """「重新计算路径」: ask the model, then move the CBZ -- never repack."""

    @pytest.mark.asyncio
    async def test_the_job_asks_the_model_and_moves_the_file(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        job_id = await _seed_packed(database, tmp_path)
        ai = _FakeAi()
        refile = _FakeRefile()
        conversion = _conversion(database, settings, tmp_path, ai, refile=refile)
        await settings.save_path_source(PATH_SOURCE_AI)

        await conversion._handle_refile_job(
            {
                "job_id": job_id,
                "candidate_id": 7,
                "refile": True,
                "refresh_ai_path": False,
            }
        )

        assert refile.calls == [(7, "同人志/作者/作品.cbz")]
        assert len(ai.calls) == 1
        # The CBZ that came out is the one that went in: no packer ran.
        assert (tmp_path / "library" / "旧" / "书.cbz").exists()
        state, code, _ = await asyncio.to_thread(
            conversion._read_final_state_sync, job_id
        )
        assert state == "CONVERSION_COMPLETED"
        assert code is None
        details = await _job_details(database, job_id)
        assert details["refiled"] is True
        assert details["moved"] is True

    @pytest.mark.asyncio
    async def test_a_current_cache_answers_without_asking_again(
        self, tmp_path: Path
    ) -> None:
        """A stale-cache job asks because the fingerprint misses, not by rote.

        The distinction matters for cost: a book the model already answered for
        is moved on that answer, and only 强制 / 「重新起个名字」 pay for a second
        question.
        """
        database, settings = await _seed(tmp_path)
        job_id = await _seed_packed(database, tmp_path)
        ai = _FakeAi()
        conversion = _conversion(
            database, settings, tmp_path, ai, refile=_FakeRefile()
        )
        await settings.save_path_source(PATH_SOURCE_AI)
        # Primed with the metadata the job will actually read (this book has
        # none), so the fingerprint is the one the job recomputes.
        await AiPathService(database, ai, settings).resolve(7, [])
        assert len(ai.calls) == 1

        await conversion._handle_refile_job(
            {
                "job_id": job_id,
                "candidate_id": 7,
                "refile": True,
                "refresh_ai_path": False,
            }
        )
        assert len(ai.calls) == 1

    @pytest.mark.asyncio
    async def test_a_refresh_job_re_asks_a_book_the_cache_could_answer(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        job_id = await _seed_packed(database, tmp_path)
        ai = _FakeAi()
        conversion = _conversion(
            database, settings, tmp_path, ai, refile=_FakeRefile()
        )
        await settings.save_path_source(PATH_SOURCE_AI)
        await AiPathService(database, ai, settings).resolve(7, [])
        assert len(ai.calls) == 1

        await conversion._handle_refile_job(
            {
                "job_id": job_id,
                "candidate_id": 7,
                "refile": True,
                "refresh_ai_path": True,
            }
        )
        assert len(ai.calls) == 2

    @pytest.mark.asyncio
    async def test_a_failure_without_the_fallback_parks_the_job(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        job_id = await _seed_packed(database, tmp_path)
        ai = _FakeAi(failure=AiError(AI_PATH_UNAVAILABLE, "所有 AI 模型都不可用"))
        refile = _FakeRefile()
        conversion = _conversion(database, settings, tmp_path, ai, refile=refile)
        await settings.save_path_source(PATH_SOURCE_AI)

        await conversion._handle_refile_job(
            {
                "job_id": job_id,
                "candidate_id": 7,
                "refile": True,
                "refresh_ai_path": False,
            }
        )

        state, code, message = await asyncio.to_thread(
            conversion._read_final_state_sync, job_id
        )
        assert state == "CONVERSION_WAITING_PATH"
        assert code == AI_PATH_UNAVAILABLE
        assert refile.calls == []

    @pytest.mark.asyncio
    async def test_a_refused_move_fails_the_job_with_its_own_code(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        job_id = await _seed_packed(database, tmp_path)
        ai = _FakeAi()
        refusal = ArchivedWorkError("FILE_MOVE_FAILED", "移动文件失败")
        conversion = _conversion(
            database, settings, tmp_path, ai, refile=_FakeRefile(failure=refusal)
        )
        await settings.save_path_source(PATH_SOURCE_AI)

        await conversion._handle_refile_job(
            {
                "job_id": job_id,
                "candidate_id": 7,
                "refile": True,
                "refresh_ai_path": False,
            }
        )

        state, code, message = await asyncio.to_thread(
            conversion._read_final_state_sync, job_id
        )
        assert state == "CONVERSION_FAILED"
        assert code == "FILE_MOVE_FAILED"
        assert message == "移动文件失败"

    @pytest.mark.asyncio
    async def test_a_re_file_does_not_need_the_source_archive(
        self, tmp_path: Path
    ) -> None:
        """The ordinary case: 「保留原始压缩包」 is off, so the source is gone.

        A pack would be refused here, and it must be -- it has nothing to
        compress. A re-file has the published CBZ, which is the thing it moves.
        """
        database, settings = await _seed(tmp_path)
        await _seed_packed(database, tmp_path)
        conversion = _conversion(database, settings, tmp_path, None)

        with pytest.raises(ConversionError) as caught:
            await conversion.enqueue_for_candidate(7)
        assert caught.value.code == "ARCHIVE_NOT_READY"

        job_id = await conversion.enqueue_for_candidate(7, refile=True)
        assert job_id > 0

    @pytest.mark.asyncio
    async def test_a_re_file_for_an_unpacked_book_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        conversion = _conversion(database, settings, tmp_path, None)

        with pytest.raises(ConversionError) as caught:
            await conversion.enqueue_for_candidate(7, refile=True)
        assert caught.value.code == "WORK_NOT_PACKAGED"

    @pytest.mark.asyncio
    async def test_a_refile_job_is_claimed_with_its_flags(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        conversion = _conversion(database, settings, tmp_path, None)
        # The enqueue needs the download readiness gate, which this book has no
        # archive for; the flags themselves are what is under test, so the row is
        # seeded rather than enqueued.
        await _seed_pending_job(database, {"refile": True})
        claimed = await asyncio.to_thread(conversion._claim_pending_job_sync)
        assert claimed is not None
        assert claimed["refile"] is True
        assert claimed["refresh_ai_path"] is False

    @pytest.mark.asyncio
    async def test_an_ordinary_pack_job_claims_with_both_flags_off(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        await _candidate(database)
        conversion = _conversion(database, settings, tmp_path, None)
        await _seed_pending_job(database, {"refresh_ai_path": True})
        claimed = await asyncio.to_thread(conversion._claim_pending_job_sync)
        assert claimed is not None
        assert claimed["refile"] is False
        assert claimed["refresh_ai_path"] is True

