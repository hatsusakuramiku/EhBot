"""The AI series decision (R83): parsing, the service, and the pack-time order.

The contract worth naming:

* a stored `Series` value always wins -- an operator's edit is never overridden
  by a model, switch or no switch;
* an AI-decided archive path already carries the series as the folder the book
  is filed under, so no second model call is made;
* 「开了自动添加系列但 AI 不可用」 is never silent -- a first pack parks
  (`AI_SERIES_UNAVAILABLE`), a repack is refused before a task exists.

The model is a fake object the way `test_ai_paths` uses one: the question here
is which branch ran, not what an HTTP client sent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

from app.ai.errors import AI_SERIES_INVALID, AiError
from app.ai.models import (
    AiAnswer,
    AiModelChainEntry,
    AiPathSuggestion,
    AiProvider,
    AiProviderModel,
)
from app.ai.paths import AiPathService
from app.ai.series import AiSeriesService, answer_series
from app.archive.service import PATH_SOURCE_AI, ArchiveSettingsService
from app.conversion.comicinfo import build_comicinfo_xml
from app.conversion.convert import ConversionError
from app.conversion.naming import LibraryPathError
from app.conversion.service import ConversionService
from app.db.database import Database


def _rows(**fields: object) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(field_name=name, field_value=value)
        for name, value in fields.items()
    ]


def _chain(count: int = 1) -> tuple[AiModelChainEntry, ...]:
    provider = AiProvider(provider_id=1, name="本地", code="openai", base_url="http://x")
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
    """A scripted `AiProviderService`: one answer, one call log."""

    def __init__(self, text: str = '{"series": "系列A"}', *, failure: AiError | None = None):
        self.text = text
        self.failure = failure
        self.calls: list[list[dict[str, str]]] = []
        self._chain = _chain()

    async def effective_chain(self, scope: str = "default") -> tuple[AiModelChainEntry, ...]:
        return self._chain

    async def complete(self, messages, *, validate=None, stream=None) -> AiAnswer:
        self.calls.append([dict(message) for message in messages])
        if self.failure is not None:
            raise self.failure
        if validate is not None:
            validate(self.text)
        return AiAnswer(
            text=self.text,
            provider_id=1,
            provider_name="本地",
            model_name="m0",
            key_id=1,
        )


async def _seed(tmp_path: Path, *, candidate_id: int = 7) -> tuple[Database, ArchiveSettingsService]:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    def _write() -> None:
        with database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO candidates (id, status) VALUES (?, 'APPROVED')",
                (candidate_id,),
            )

    await asyncio.to_thread(_write)
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
) -> ConversionService:
    return ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        settings_service=settings,
        data_path=tmp_path / "data",
        ai_service=ai,
    )


class TestAnswerSeries:
    def test_a_plain_object_yields_the_name(self) -> None:
        assert answer_series('{"series": "隣の喘ぎ声"}') == "隣の喘ぎ声"

    def test_a_fenced_or_padded_body_is_still_read(self) -> None:
        assert answer_series('好的：\n```json\n{"series": "X"}\n```') == "X"

    def test_an_empty_series_is_no_series(self) -> None:
        assert answer_series('{"series": ""}') is None
        assert answer_series('{"series": "   "}') is None

    def test_a_missing_key_is_no_series(self) -> None:
        assert answer_series("{}") is None

    def test_a_null_series_is_no_series(self) -> None:
        assert answer_series('{"series": null}') is None

    def test_a_non_string_series_is_a_refusal(self) -> None:
        with pytest.raises(AiError) as raised:
            answer_series('{"series": 5}')
        assert raised.value.code == AI_SERIES_INVALID

    def test_a_body_without_json_is_a_refusal(self) -> None:
        with pytest.raises(AiError):
            answer_series("这个作品没有系列")


class TestAiSeriesService:
    @pytest.mark.asyncio
    async def test_it_asks_the_chain_and_returns_the_name(self) -> None:
        ai = _FakeAi('{"series": "系列A"}')
        service = AiSeriesService(ai)
        assert await service.resolve(_rows(Title="T")) == "系列A"
        assert ai.calls and ai.calls[0][0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_none(self) -> None:
        service = AiSeriesService(_FakeAi('{"series": ""}'))
        assert await service.resolve(_rows(Title="T")) is None

    @pytest.mark.asyncio
    async def test_a_chain_failure_propagates(self) -> None:
        service = AiSeriesService(_FakeAi(failure=AiError("X", "boom")))
        with pytest.raises(AiError):
            await service.resolve(_rows(Title="T"))


class TestResolveSeries:
    @pytest.mark.asyncio
    async def test_a_stored_series_wins_with_the_switch_off(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _conversion(database, settings, tmp_path, None)
        result = await service._resolve_series(  # noqa: SLF001
            7, _rows(Series="元数据系列", Title="T")
        )
        assert result == "元数据系列"

    @pytest.mark.asyncio
    async def test_the_switch_off_without_metadata_adds_nothing(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _conversion(database, settings, tmp_path, None)
        assert await service._resolve_series(7, _rows(Title="T")) is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_an_ai_path_uses_its_folder_without_a_model_call(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_path_source(PATH_SOURCE_AI)
        await settings.save_auto_series(True)
        ai = _FakeAi()
        rows = _rows(Title="T")
        # A fingerprint that matches today's prompt / chain / metadata, because a
        # stale row is deliberately ignored.
        cached_path = AiPathService(database, ai, settings)
        fingerprint = await cached_path.fingerprint(rows)
        await database.save_ai_path_suggestion(
            AiPathSuggestion(
                candidate_id=7,
                fingerprint=fingerprint,
                prompt_hash="p",
                relative_path="同人志/社团/系列B/作品.cbz",
                directory="同人志/社团/系列B",
                filename="作品",
                provider_id=1,
                model_name="m0",
            )
        )
        service = _conversion(database, settings, tmp_path, ai)
        result = await service._resolve_series(7, rows)  # noqa: SLF001
        assert result == "系列B"
        assert ai.calls == []

    @pytest.mark.asyncio
    async def test_the_switch_on_asks_the_model_and_writes_it_back(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        ai = _FakeAi('{"series": "系列C"}')
        service = _conversion(database, settings, tmp_path, ai)
        result = await service._resolve_series(7, _rows(Title="T"))  # noqa: SLF001
        assert result == "系列C"
        assert len(ai.calls) == 1
        entries = await database.list_metadata(7)
        stored = [entry for entry in entries if entry.field_name == "Series"]
        assert [entry.field_value for entry in stored] == ["系列C"]
        assert stored[0].value_source == "AI"

    @pytest.mark.asyncio
    async def test_an_empty_model_answer_is_not_written_back(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        service = _conversion(database, settings, tmp_path, _FakeAi('{"series": ""}'))
        assert await service._resolve_series(7, _rows(Title="T")) is None  # noqa: SLF001
        entries = await database.list_metadata(7)
        assert [e for e in entries if e.field_name == "Series"] == []

    @pytest.mark.asyncio
    async def test_the_switch_on_without_ai_refuses(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        service = _conversion(database, settings, tmp_path, None)
        with pytest.raises(LibraryPathError) as raised:
            await service._resolve_series(7, _rows(Title="T"))  # noqa: SLF001
        assert raised.value.code == "AI_SERIES_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_a_model_failure_becomes_an_unavailable_refusal(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        service = _conversion(database, settings, tmp_path, _FakeAi(failure=AiError("X", "boom")))
        with pytest.raises(LibraryPathError) as raised:
            await service._resolve_series(7, _rows(Title="T"))  # noqa: SLF001
        assert raised.value.code == "AI_SERIES_UNAVAILABLE"


class TestRefuseBeforeEnqueue:
    @pytest.mark.asyncio
    async def test_a_repack_without_ai_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        with database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO download_jobs (id, candidate_id, idempotency_key, provider, state) "
                "VALUES (1, 7, 'convert:7', 'CONVERSION', 'CONVERSION_COMPLETED')"
            )
            connection.execute(
                "INSERT INTO artifacts (job_id, artifact_type, path) "
                "VALUES (1, 'CBZ', '/library/book.cbz')"
            )
        service = _conversion(database, settings, tmp_path, None)
        with pytest.raises(ConversionError) as raised:
            await service.enqueue_for_candidate(7)
        assert raised.value.code == "SERIES_AI_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_a_first_pack_is_not_refused_here(self, tmp_path: Path) -> None:
        """No CBZ yet: the job is created and parks later, so this must not raise
        `SERIES_AI_UNAVAILABLE` -- it raises the readiness refusal instead."""
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        service = _conversion(database, settings, tmp_path, None)
        with pytest.raises(ConversionError) as raised:
            await service.enqueue_for_candidate(7)
        assert raised.value.code == "ARCHIVE_NOT_READY"

    @pytest.mark.asyncio
    async def test_a_stored_series_lets_the_repack_through(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        await settings.save_auto_series(True)
        with database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO download_jobs (id, candidate_id, idempotency_key, provider, state) "
                "VALUES (1, 7, 'telegram:1:a', 'TELEGRAM', 'COMPLETED')"
            )
            connection.execute(
                "INSERT INTO artifacts (job_id, artifact_type, path) "
                "VALUES (1, 'ARCHIVE', '/work/a.zip')"
            )
            connection.execute(
                "INSERT INTO artifacts (job_id, artifact_type, path) "
                "VALUES (1, 'CBZ', '/library/book.cbz')"
            )
            connection.execute(
                "INSERT INTO metadata_values (candidate_id, field_name, field_value, value_source) "
                "VALUES (7, 'Series', '系列X', 'OPERATOR')"
            )
        service = _conversion(database, settings, tmp_path, None)
        job_id = await service.enqueue_for_candidate(7)
        assert job_id == 2


class TestComicInfoSeries:
    def test_a_resolved_series_fills_both_fields(self) -> None:
        xml = build_comicinfo_xml(
            title="作品", series="系列名", parody="原作", japanese_title="日本語"
        ).decode("utf-8")
        assert "<Series>系列名</Series>" in xml
        assert "<LocalizedSeries>系列名</LocalizedSeries>" in xml

    def test_without_a_series_the_old_fallbacks_stand(self) -> None:
        xml = build_comicinfo_xml(
            title="作品", parody="原作", japanese_title="日本語"
        ).decode("utf-8")
        assert "<Series>原作</Series>" in xml
        assert "<LocalizedSeries>日本語</LocalizedSeries>" in xml

    def test_without_parody_the_title_is_the_last_fallback(self) -> None:
        xml = build_comicinfo_xml(title="作品").decode("utf-8")
        assert "<Series>作品</Series>" in xml
        assert "<LocalizedSeries>作品</LocalizedSeries>" in xml
