"""The AI provider catalogue: validation, rotation, fallback, verification.

The HTTP transport is faked at the client factory, not at `httpx`, because the
rules under test are 「哪把 Key、哪个模型」 rather than 「怎么发请求」: a stub client
returns a scripted answer or a scripted refusal, and the assertions are about the
order the service tried things in and what it wrote down afterwards. The one
place that really parses a response -- `extract_json_object` -- is a pure
function and is tested directly.

Two invariants get their own tests because they are the ones a refactor is most
likely to break silently: a rejected key must be cooled down and skipped on the
next request (not merely retried), and the whole chain failing must name every
entry it tried, because that sentence is what the 需干预 state shows an operator.

`_ready_chain` verifies the models before saving the chain, and the stub's
behaviour can tell a verification request from a real one by its first message:
without that, a test that wants 「primary fails at pack time」 would also fail the
verification, and the chain save would be refused before the interesting part.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Sequence

import httpx
import pytest

from app.ai.client import (
    AI_AUTH,
    AI_BAD_RESPONSE,
    AI_PARAM_REJECTED,
    AI_RATE_LIMIT,
    AI_REQUEST_REJECTED,
    AI_TIMEOUT,
    AiClientError,
    OpenAiCompatibleClient,
    _classify_status,
    _content_of,
    _error_envelope,
)
from app.ai.errors import (
    AI_CHAIN_DUPLICATE,
    AI_CHAIN_EMPTY,
    AI_CHAIN_ENTRY_MISSING,
    AI_CHAIN_UNAVAILABLE,
    AI_KEY_INVALID,
    AI_MODEL_INVALID,
    AI_MODEL_NOT_FOUND,
    AI_NO_KEY,
    AI_PARAMS_INVALID,
    AI_PROVIDER_INVALID,
    AI_PROVIDER_NAME_TAKEN,
    AiError,
)
from app.ai.models import (
    CHAIN_SCOPE_ARCHIVE_PATH,
    CHAIN_SCOPE_CANDIDATE,
    CHAIN_SCOPE_DEFAULT,
    MODEL_SOURCE_CUSTOM,
    MODEL_SOURCE_DEFAULT,
    AiRequestParams,
)
from app.ai.service import AiProviderService, extract_json_object
from app.archive.service import ArchiveSettingsService
from app.archive.vault import decrypt_password
from app.db.database import Database

#: The first message of the verification request; the stub uses it to answer a
#: probe with a probe's reply while still failing the model for real requests.
PROBE_PREFIX = "You are a connectivity probe"


def _is_probe(messages: Sequence[dict[str, str]]) -> bool:
    return bool(messages) and str(messages[0].get("content", "")).startswith(
        PROBE_PREFIX
    )


async def _seed(tmp_path: Path) -> tuple[Database, ArchiveSettingsService]:
    """A fresh migrated database and the settings service that holds the key."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    settings = ArchiveSettingsService(
        database,
        tmp_path / "work",
        default_library_path=tmp_path / "library",
        default_work_path=tmp_path / "work",
    )
    return database, settings


Behavior = Callable[[str, str, Sequence[dict[str, str]]], str]


def _restart_rotation(service: AiProviderService) -> None:
    """Reset the in-memory rotation cursor.

    The cursor is deliberately not durable -- a restart starts at the first key
    -- so a test that wants to observe 「第一把 Key 先被尝试」 says so explicitly
    rather than depending on where the verification calls left the cursor.
    """
    service._cursors.clear()


class _StubClient:
    """A chat client whose answer is decided by (key, model, messages).

    `stream` is accepted and ignored: it is a transport choice, and a stub that
    refused it would make every rotation test depend on the shape of an argument
    that has no bearing on which key answers.
    """

    def __init__(
        self, behavior: Behavior, *, api_key: str, model: str
    ) -> None:
        self._behavior = behavior
        self._api_key = api_key
        self._model = model

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        params=None,
        stream: bool = False,
    ) -> str:
        return self._behavior(self._api_key, self._model, messages)


def _factory(behavior: Behavior, seen: list[tuple[str, str, str]]):
    """A client factory that records the (base_url, key, model) it was asked for.

    Recording the key *here* rather than after the call is deliberate: it shows
    the order of attempts, including the ones that raised.
    """

    def build(
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
        max_retries: int,
        extra_headers: dict[str, str] | None = None,
    ) -> _StubClient:
        seen.append((base_url, api_key, model))
        return _StubClient(behavior, api_key=api_key, model=model)

    return build


def _success(key: str, model: str, messages: Sequence[dict[str, str]]) -> str:
    return '{"ok": true}'


def _service(
    database: Database,
    settings: ArchiveSettingsService,
    *,
    behavior: Behavior | None = None,
    seen: list[tuple[str, str, str]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AiProviderService:
    return AiProviderService(
        database,
        settings,
        http_client=httpx.AsyncClient(transport=transport),
        client_factory=_factory(behavior or _success, seen if seen is not None else []),
    )


def _provider_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "provider_id": None,
        "name": "本地",
        "code": "openai",
        "base_url": "http://localhost:11434/v1",
        "timeout_seconds": 30,
        "max_retries": 1,
        "enabled": True,
    }
    values.update(overrides)
    return values


async def _catalogue(
    service: AiProviderService,
    *,
    keys: Sequence[str] = ("key-one",),
    model_names: Sequence[str] = ("model-a",),
) -> tuple[int, list[int], list[int]]:
    """One enabled provider with `keys` and `model_names`; returns the ids."""
    provider = await service.save_provider(_provider_values())
    key_ids = [
        (
            await service.add_key(
                provider.provider_id, label=name, api_key=name
            )
        ).key_id
        for name in keys
    ]
    model_ids = [
        (await service.add_model(provider.provider_id, name)).model_id
        for name in model_names
    ]
    return provider.provider_id, key_ids, model_ids


async def _ready_chain(
    service: AiProviderService,
    *,
    keys: Sequence[str] = ("key-one",),
    model_names: Sequence[str] = ("model-a",),
) -> tuple[int, list[int], list[int]]:
    """A catalogue whose models are verified and installed as the chain."""
    provider_id, key_ids, model_ids = await _catalogue(
        service, keys=keys, model_names=model_names
    )
    for model_id in model_ids:
        await service.verify_model(model_id)
    await service.save_chain(model_ids)
    return provider_id, key_ids, model_ids


# ---------------------------------------------------------------------------
#  extract_json_object
# ---------------------------------------------------------------------------


class TestExtractJsonObject:
    def test_a_bare_object_parses(self) -> None:
        assert extract_json_object('{"directory": "a", "filename": "b"}') == {
            "directory": "a",
            "filename": "b",
        }

    def test_a_fenced_block_parses(self) -> None:
        """Models wrap JSON in ``` fences constantly; refusing that would fail
        an answer that plainly is the answer."""
        assert extract_json_object('```json\n{"ok": true}\n```') == {"ok": True}

    def test_surrounding_prose_is_tolerated(self) -> None:
        assert extract_json_object('好的，这是结果：{"ok": true} 希望有帮助。') == {
            "ok": True
        }

    @pytest.mark.parametrize("text", ["", "   ", "抱歉，我不能这么做。"])
    def test_no_object_is_a_refusal(self, text: str) -> None:
        with pytest.raises(AiError) as caught:
            extract_json_object(text)
        assert caught.value.code == AI_BAD_RESPONSE

    def test_malformed_json_is_a_refusal(self) -> None:
        with pytest.raises(AiError) as caught:
            extract_json_object('{"ok": tru}')
        assert caught.value.code == AI_BAD_RESPONSE

    def test_a_non_object_is_a_refusal(self) -> None:
        with pytest.raises(AiError) as caught:
            extract_json_object("[1, 2, 3]")
        assert caught.value.code == AI_BAD_RESPONSE


# ---------------------------------------------------------------------------
#  Provider validation
# ---------------------------------------------------------------------------


class TestProviderValidation:
    @pytest.mark.asyncio
    async def test_a_nameless_provider_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.save_provider(_provider_values(name="  "))
        assert caught.value.code == AI_PROVIDER_INVALID

    @pytest.mark.asyncio
    async def test_a_url_without_a_host_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.save_provider(_provider_values(base_url="ftp://x"))
        assert caught.value.code == AI_PROVIDER_INVALID

    @pytest.mark.asyncio
    async def test_an_out_of_range_timeout_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.save_provider(_provider_values(timeout_seconds=9999))
        assert caught.value.code == AI_PROVIDER_INVALID

    @pytest.mark.asyncio
    async def test_a_duplicate_name_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        await service.save_provider(_provider_values())
        with pytest.raises(AiError) as caught:
            await service.save_provider(_provider_values())
        assert caught.value.code == AI_PROVIDER_NAME_TAKEN

    @pytest.mark.asyncio
    async def test_editing_a_provider_keeps_its_identity(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        updated = await service.save_provider(
            _provider_values(
                provider_id=provider.provider_id,
                name="改名",
                base_url="https://api.example.com/v1/",
            )
        )
        assert updated.provider_id == provider.provider_id
        assert updated.name == "改名"
        assert updated.base_url == "https://api.example.com/v1"

    @pytest.mark.asyncio
    async def test_deleting_a_provider_takes_its_keys_and_models(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider_id, _, model_ids = await _catalogue(service)
        await service.delete_provider(provider_id)
        assert await service.providers() == ()
        assert await service.keys(provider_id) == ()
        assert await service.model(model_ids[0]) is None
        assert await service.chain() == ()


# ---------------------------------------------------------------------------
#  Keys
# ---------------------------------------------------------------------------


class TestKeys:
    @pytest.mark.asyncio
    async def test_an_empty_key_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        with pytest.raises(AiError) as caught:
            await service.add_key(provider.provider_id, label="", api_key="  ")
        assert caught.value.code == AI_KEY_INVALID

    @pytest.mark.asyncio
    async def test_a_stored_key_is_encrypted_and_decryptable(
        self, tmp_path: Path
    ) -> None:
        """The plaintext must not be in the row, and must survive a round trip.

        Asserting both halves matters: an envelope that is not the plaintext but
        also cannot be opened would pass a leak check and fail every request.
        """
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        key = await service.add_key(
            provider.provider_id, label="主", api_key="sk-super-secret"
        )
        cipher = await database.ai_provider_key_cipher(key.key_id)
        assert cipher is not None
        assert "sk-super-secret" not in cipher
        master = await settings.master_key()
        assert decrypt_password(master, cipher) == "sk-super-secret"
        # The DTO the page sees carries no secret at all.
        listed = await service.keys(provider.provider_id)
        assert listed[0].label == "主"
        assert not hasattr(listed[0], "cipher")

    @pytest.mark.asyncio
    async def test_a_disabled_key_does_not_rotate(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []
        service = _service(database, settings, seen=seen)
        provider_id, key_ids, model_ids = await _catalogue(
            service, keys=("first", "second")
        )
        for model_id in model_ids:
            await service.verify_model(model_id)
        await service.save_chain(model_ids)
        await service.set_key_enabled(key_ids[0], False)
        seen.clear()
        await service.complete([{"role": "user", "content": "hi"}])
        assert [call[1] for call in seen] == ["second"]


# ---------------------------------------------------------------------------
#  Rotation and fallback
# ---------------------------------------------------------------------------


class TestRotation:
    @pytest.mark.asyncio
    async def test_a_rejected_key_is_cooled_down_and_skipped(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            if key == "bad":
                raise AiClientError(AI_AUTH, "拒绝", key_fault=True, status=401)
            return '{"ok": true}'

        service = _service(database, settings, behavior=behavior, seen=seen)
        provider_id, key_ids, model_ids = await _catalogue(
            service, keys=("bad", "good")
        )
        for model_id in model_ids:
            await service.verify_model(model_id)
        # The bad key is cooled by the verification attempts, so it must be
        # cleared out of the cooldown to be the first candidate again.
        await database.mark_ai_key_used(key_ids[0])
        await database.mark_ai_key_used(key_ids[1])
        await service.save_chain(model_ids)
        _restart_rotation(service)
        seen.clear()

        answer = await service.complete([{"role": "user", "content": "hi"}])
        assert answer.key_id == key_ids[1]
        assert answer.text == '{"ok": true}'
        # The rejected key was tried first -- round-robin starts at the top --
        # and the good one after it.
        assert [call[1] for call in seen] == ["bad", "good"]

        stored = {key.key_id: key for key in await service.keys(provider_id)}
        assert stored[key_ids[0]].failures >= 1
        assert stored[key_ids[0]].cooldown_until is not None

        # The next request never sees the parked key at all.
        seen.clear()
        await service.complete([{"role": "user", "content": "hi"}])
        assert [call[1] for call in seen] == ["good"]

    @pytest.mark.asyncio
    async def test_a_transient_failure_moves_to_the_next_key(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            if key == "slow":
                raise AiClientError(AI_TIMEOUT, "超时", retryable=True)
            return "ok"

        service = _service(database, settings, behavior=behavior, seen=seen)
        provider_id, key_ids, model_ids = await _ready_chain(
            service, keys=("slow", "ok")
        )
        _restart_rotation(service)
        seen.clear()
        await service.complete([{"role": "user", "content": "hi"}])
        assert [call[1] for call in seen] == ["slow", "ok"]
        # A timeout is not a credential fault, so nothing is parked for it.
        stored = {key.key_id: key for key in await service.keys(provider_id)}
        assert stored[key_ids[0]].cooldown_until is None


class TestChainFallback:
    @pytest.mark.asyncio
    async def test_the_primary_answers_when_it_can(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []
        service = _service(database, settings, seen=seen)
        await _ready_chain(service, model_names=("primary", "backup"))
        seen.clear()

        answer = await service.complete([{"role": "user", "content": "hi"}])
        assert answer.model_name == "primary"
        assert [call[2] for call in seen] == ["primary"]

    @pytest.mark.asyncio
    async def test_a_failed_primary_falls_through_to_the_backup(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            if model == "primary":
                raise AiClientError("AI_SERVER_ERROR", "上游挂了", retryable=True)
            return "backup-answered"

        service = _service(database, settings, behavior=behavior, seen=seen)
        await _ready_chain(service, model_names=("primary", "backup"))
        seen.clear()

        answer = await service.complete([{"role": "user", "content": "hi"}])
        assert answer.text == "backup-answered"
        assert answer.model_name == "backup"
        assert [call[2] for call in seen] == ["primary", "backup"]

    @pytest.mark.asyncio
    async def test_every_model_failing_names_them_in_one_refusal(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            raise AiClientError(AI_TIMEOUT, f"{model} 超时", retryable=True)

        service = _service(database, settings, behavior=behavior)
        await _ready_chain(service, model_names=("first", "second"))

        with pytest.raises(AiError) as caught:
            await service.complete([{"role": "user", "content": "hi"}])
        assert caught.value.code == AI_CHAIN_UNAVAILABLE
        # 「哪一个供应商/模型失败了」 is the sentence the 需干预 state shows.
        assert "first" in caught.value.public_message
        assert "second" in caught.value.public_message

    @pytest.mark.asyncio
    async def test_a_disabled_model_is_skipped(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service, model_names=("keep", "parked"))
        await service.set_model_enabled(model_ids[1], False)

        answer = await service.complete([{"role": "user", "content": "hi"}])
        assert answer.model_name == "keep"

    @pytest.mark.asyncio
    async def test_an_empty_chain_refuses_before_any_request(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []
        service = _service(database, settings, seen=seen)
        await _catalogue(service)
        with pytest.raises(AiError) as caught:
            await service.complete([{"role": "user", "content": "hi"}])
        assert caught.value.code == AI_CHAIN_EMPTY
        assert seen == []


class TestAnswerValidation:
    """`complete(validate=...)`: an unusable *answer* walks the chain like a timeout.

    The path feature cannot accept 「HTTP 200, prose」 -- and that failure must
    reach the next model, because that is what a fallback is for. Keeping the
    check inside `complete` is what makes 「返回非法 JSON 也算失败」 true for the
    whole chain rather than only for the model that happened to answer first.
    """

    @pytest.mark.asyncio
    async def test_a_rejected_answer_falls_through_to_the_backup(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            return "primary prose" if model == "primary" else '{"directory": "", "filename": "ok"}'

        service = _service(database, settings, behavior=behavior, seen=seen)
        await _ready_chain(service, model_names=("primary", "backup"))
        seen.clear()

        def accept(text: str) -> None:
            if not text.startswith("{"):
                raise AiError(AI_BAD_RESPONSE, "不是 JSON")

        answer = await service.complete(
            [{"role": "user", "content": "hi"}], validate=accept
        )
        assert answer.model_name == "backup"
        assert [call[2] for call in seen] == ["primary", "backup"]

    @pytest.mark.asyncio
    async def test_every_answer_being_rejected_is_one_refusal(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        await _ready_chain(service, model_names=("first", "second"))

        def reject(text: str) -> None:
            raise AiError(AI_BAD_RESPONSE, "不是 JSON")

        with pytest.raises(AiError) as caught:
            await service.complete(
                [{"role": "user", "content": "hi"}], validate=reject
            )
        assert caught.value.code == AI_CHAIN_UNAVAILABLE
        assert "first" in caught.value.public_message


# ---------------------------------------------------------------------------
#  Save-time refusal of the chain
# ---------------------------------------------------------------------------


class TestChainConfiguration:
    """配置不阻塞：能不能用由「测试」按钮说，而不是由保存时的门禁说。"""

    @pytest.mark.asyncio
    async def test_an_unverified_model_can_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        chain = await service.save_chain(model_ids)
        assert [entry.model.model_id for entry in chain] == model_ids

    @pytest.mark.asyncio
    async def test_a_disabled_model_can_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        await service.set_model_enabled(model_ids[0], False)
        chain = await service.save_chain(model_ids)
        assert [entry.model.model_id for entry in chain] == model_ids

    @pytest.mark.asyncio
    async def test_a_provider_without_a_usable_key_can_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        """保存不再检查 Key：端点临时故障时也要能把配置写完。

        运行时仍然按 Key 轮询/回落（见 `TestRotation`），所以这条只断言
        「写得进去」，不断言「跑得通」。
        """
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, key_ids, model_ids = await _catalogue(service)
        await service.set_key_enabled(key_ids[0], False)
        chain = await service.save_chain(model_ids)
        assert [entry.model.model_id for entry in chain] == model_ids

    @pytest.mark.asyncio
    async def test_a_duplicate_entry_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        with pytest.raises(AiError) as caught:
            await service.save_chain([model_ids[0], model_ids[0]])
        assert caught.value.code == AI_CHAIN_DUPLICATE

    @pytest.mark.asyncio
    async def test_an_unknown_model_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.save_chain([999])
        assert caught.value.code == AI_MODEL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_reordering_and_removing_keep_the_order(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(
            service, model_names=("a", "b", "c")
        )

        chain = await service.shift_chain(model_ids[2], -1)
        assert [entry.model.model_id for entry in chain] == [
            model_ids[0],
            model_ids[2],
            model_ids[1],
        ]
        assert chain[0].is_primary
        chain = await service.shift_chain(model_ids[2], -5)
        assert [entry.model.model_id for entry in chain][0] == model_ids[2]
        chain = await service.remove_from_chain(model_ids[2])
        assert [entry.model.model_id for entry in chain] == [
            model_ids[0],
            model_ids[1],
        ]

    @pytest.mark.asyncio
    async def test_promoting_a_model_makes_it_primary_and_appends_unknown_ones(
        self, tmp_path: Path
    ) -> None:
        """「设为主力」一击生效：不在列表里的模型也会被加进来。"""
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service, model_names=("a", "b"))
        # 空列表上「设为主力」= 就这一个。
        chain = await service.set_chain_primary(model_ids[1])
        assert [entry.model.model_id for entry in chain] == [model_ids[1]]
        assert chain[0].is_primary
        # 已经在列表里的模型被提为主力，原来的主力顺位后移。
        chain = await service.append_to_chain(model_ids[0])
        chain = await service.set_chain_primary(model_ids[1])
        assert [entry.model.model_id for entry in chain] == [
            model_ids[1],
            model_ids[0],
        ]
        assert [entry.is_primary for entry in chain] == [True, False]


class TestChainScopes:
    """一张全局默认列表，各页面按需覆盖；不配置就继承默认。"""

    @pytest.mark.asyncio
    async def test_the_archive_path_scope_inherits_the_default(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service)
        effective = await service.effective_chain(CHAIN_SCOPE_ARCHIVE_PATH)
        assert [entry.model.model_id for entry in effective] == model_ids

    @pytest.mark.asyncio
    async def test_an_explicit_custom_list_wins(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service, model_names=("a", "b"))
        await settings.save_ai_model_source("custom")
        await service.save_chain([model_ids[1]], CHAIN_SCOPE_ARCHIVE_PATH)
        effective = await service.effective_chain(CHAIN_SCOPE_ARCHIVE_PATH)
        assert [entry.model.model_id for entry in effective] == [model_ids[1]]
        # 全局默认没被动过：换回跟随即回到原列表。
        await settings.save_ai_model_source("default")
        effective = await service.effective_chain(CHAIN_SCOPE_ARCHIVE_PATH)
        assert [entry.model.model_id for entry in effective] == model_ids

    @pytest.mark.asyncio
    async def test_an_empty_custom_list_is_loud_instead_of_silent(
        self, tmp_path: Path
    ) -> None:
        """选了「本页单独指定」却没填：报空，而不是偷偷用回全局默认。"""
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        await _ready_chain(service)
        await settings.save_ai_model_source("custom")
        assert await service.effective_chain(CHAIN_SCOPE_ARCHIVE_PATH) == ()

    @pytest.mark.asyncio
    async def test_the_default_scope_never_inherits_anything(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service)
        await service.save_chain([model_ids[0]], CHAIN_SCOPE_ARCHIVE_PATH)
        assert await service.effective_chain(CHAIN_SCOPE_DEFAULT) != ()
        assert await service.chain(CHAIN_SCOPE_ARCHIVE_PATH) != ()


class _MutableSource:
    """A stand-in for `SystemSettingsService.ai_candidate_model_source`."""

    def __init__(self, value: str = MODEL_SOURCE_DEFAULT) -> None:
        self.value = value

    async def read(self) -> str:
        return self.value


class TestScopeRegistry:
    """R52: a feature scope is a registration, not a hard-coded `if`."""

    @pytest.mark.asyncio
    async def test_a_registered_scope_inherits_until_it_says_custom(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service, model_names=("a", "b"))
        source = _MutableSource()
        service.register_scope_source(CHAIN_SCOPE_CANDIDATE, source.read)

        inherited = await service.effective_chain(CHAIN_SCOPE_CANDIDATE)
        assert [entry.model.model_id for entry in inherited] == model_ids

        source.value = MODEL_SOURCE_CUSTOM
        await service.save_chain([model_ids[1]], CHAIN_SCOPE_CANDIDATE)
        own = await service.effective_chain(CHAIN_SCOPE_CANDIDATE)
        assert [entry.model.model_id for entry in own] == [model_ids[1]]

        source.value = MODEL_SOURCE_DEFAULT
        back = await service.effective_chain(CHAIN_SCOPE_CANDIDATE)
        assert [entry.model.model_id for entry in back] == model_ids

    @pytest.mark.asyncio
    async def test_an_unregistered_scope_follows_the_global_default(
        self, tmp_path: Path
    ) -> None:
        """A scope nobody registered has no way to say 「custom」, so it must be
        the safe 「跟随全局」 rather than an empty own chain."""
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service)
        effective = await service.effective_chain(CHAIN_SCOPE_CANDIDATE)
        assert [entry.model.model_id for entry in effective] == model_ids

    @pytest.mark.asyncio
    async def test_an_unknown_scope_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.effective_chain("nonsense")
        assert caught.value.code == AI_CHAIN_ENTRY_MISSING

    @pytest.mark.asyncio
    async def test_the_candidate_scope_never_falls_back_to_the_global_default(
        self, tmp_path: Path
    ) -> None:
        """Requirement 5: when every model of a feature's own list fails, the
        call fails -- the global default chain is never consulted."""
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            raise AiClientError(AI_TIMEOUT, f"{model} 超时", retryable=True)

        service = _service(database, settings, behavior=behavior, seen=seen)
        _, _, model_ids = await _catalogue(
            service, model_names=("global-ok", "scope-bad")
        )
        for model_id in model_ids:
            await service.verify_model(model_id)
        # 全局默认是一把能答的模型；候选判定自建列表只有一把会失败的模型。
        await service.save_chain([model_ids[0]], CHAIN_SCOPE_DEFAULT)
        source = _MutableSource(MODEL_SOURCE_CUSTOM)
        service.register_scope_source(CHAIN_SCOPE_CANDIDATE, source.read)
        await service.save_chain([model_ids[1]], CHAIN_SCOPE_CANDIDATE)
        seen.clear()

        with pytest.raises(AiError) as caught:
            await service.complete(
                [{"role": "user", "content": "hi"}],
                scope=CHAIN_SCOPE_CANDIDATE,
            )
        assert caught.value.code == AI_CHAIN_UNAVAILABLE
        assert "AI 候选判定" in caught.value.public_message
        assert "未回退全局默认" in caught.value.public_message
        # 只碰过本作用域的模型；那把能答的全局模型一次都没被调用。
        assert [call[2] for call in seen] == ["scope-bad"]
        assert "global-ok" not in {call[2] for call in seen}

    @pytest.mark.asyncio
    async def test_the_archive_path_scope_also_never_falls_back(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[tuple[str, str, str]] = []

        def behavior(key: str, model: str, messages) -> str:
            if _is_probe(messages):
                return '{"ok": true}'
            raise AiClientError(AI_TIMEOUT, f"{model} 超时", retryable=True)

        service = _service(database, settings, behavior=behavior, seen=seen)
        _, _, model_ids = await _catalogue(
            service, model_names=("global-ok", "path-bad")
        )
        for model_id in model_ids:
            await service.verify_model(model_id)
        await service.save_chain([model_ids[0]], CHAIN_SCOPE_DEFAULT)
        await settings.save_ai_model_source(MODEL_SOURCE_CUSTOM)
        await service.save_chain([model_ids[1]], CHAIN_SCOPE_ARCHIVE_PATH)
        seen.clear()

        with pytest.raises(AiError) as caught:
            await service.complete(
                [{"role": "user", "content": "hi"}],
                scope=CHAIN_SCOPE_ARCHIVE_PATH,
            )
        assert "归档路径" in caught.value.public_message
        assert "未回退全局默认" in caught.value.public_message
        assert [call[2] for call in seen] == ["path-bad"]


# ---------------------------------------------------------------------------
#  Verification
# ---------------------------------------------------------------------------


class TestVerification:
    @pytest.mark.asyncio
    async def test_a_working_model_is_marked_verified(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        result = await service.verify_model(model_ids[0])
        assert result.ok
        model = await service.model(model_ids[0])
        assert model is not None
        assert model.verified
        assert model.last_verified_at is not None
        assert model.last_verify_error is None

    @pytest.mark.asyncio
    async def test_a_refused_key_is_recorded_as_a_failed_verification(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)

        def behavior(key: str, model: str, messages) -> str:
            raise AiClientError(AI_AUTH, "Key 无效", key_fault=True, status=401)

        service = _service(database, settings, behavior=behavior)
        provider_id, key_ids, model_ids = await _catalogue(service)
        result = await service.verify_model(model_ids[0])
        assert not result.ok
        assert result.code == AI_AUTH
        model = await service.model(model_ids[0])
        assert model is not None
        assert not model.verified
        assert model.last_verify_error is not None
        assert model.last_verify_error.startswith(AI_AUTH)
        # A failed check does not park the key: the operator has to be able to
        # fix the URL and click 验证 again, and read 「鉴权失败」 rather than
        # 「没有可用的 Key」 the second time.
        key = (await service.keys(provider_id))[0]
        assert key.key_id == key_ids[0]
        assert key.cooldown_until is None
        again = await service.verify_model(model_ids[0])
        assert not again.ok
        assert again.code == AI_AUTH

    @pytest.mark.asyncio
    async def test_an_unparseable_answer_is_recorded_as_a_failure(
        self, tmp_path: Path
    ) -> None:
        """A model that answers prose instead of the requested JSON is not
        「地址不通」 -- it is a model that cannot do the job, and the page says which."""
        database, settings = await _seed(tmp_path)
        service = _service(
            database, settings, behavior=lambda key, model, messages: "抱歉，我做不到。"
        )
        _, _, model_ids = await _catalogue(service)
        result = await service.verify_model(model_ids[0])
        assert not result.ok
        assert result.code == AI_BAD_RESPONSE

    @pytest.mark.asyncio
    async def test_verification_without_a_key_is_reported(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        model = await service.add_model(provider.provider_id, "m")
        result = await service.verify_model(model.model_id)
        assert not result.ok
        assert result.code == AI_NO_KEY


# ---------------------------------------------------------------------------
#  GET /v1/models
# ---------------------------------------------------------------------------


class TestRemoteModels:
    @pytest.mark.asyncio
    async def test_the_listing_is_read_from_the_provider(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, json={"data": [{"id": "m-one"}, {"id": " m-two "}, {"nope": 1}]}
            )

        service = _service(database, settings, transport=httpx.MockTransport(handler))
        provider = await service.save_provider(_provider_values())
        await service.add_key(provider.provider_id, label="", api_key="k")
        names = await service.list_remote_models(provider.provider_id)
        assert names == ("m-one", "m-two")
        assert seen[0].url.path == "/v1/models"
        assert seen[0].headers["authorization"] == "Bearer k"

    @pytest.mark.asyncio
    async def test_an_unreachable_provider_is_reported(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        service = _service(database, settings, transport=httpx.MockTransport(handler))
        provider = await service.save_provider(_provider_values())
        await service.add_key(provider.provider_id, label="", api_key="k")
        with pytest.raises(AiError) as caught:
            await service.list_remote_models(provider.provider_id)
        assert caught.value.code == "AI_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_a_provider_without_a_key_is_reported(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        with pytest.raises(AiError) as caught:
            await service.list_remote_models(provider.provider_id)
        assert caught.value.code == AI_NO_KEY


def test_the_key_dto_carries_no_secret() -> None:
    """Guard against a later change putting the cipher on the row.

    `AiProviderKey` carries no secret, which is what makes the settings page's
    「永不回显」 a property of the type rather than a rule the template follows.
    """
    from app.ai.models import AiProviderKey

    fields = set(AiProviderKey.__dataclass_fields__)
    assert "cipher" not in fields
    assert not any(name.endswith("credential") for name in fields)


def _sse_body(*chunks: dict) -> bytes:
    """An OpenAI-shaped SSE body, built from dicts so no quoting is guessed.

    Hand-written JSON in a test string is how a stray escape turns into a body
    the parser is right to reject -- the bug the test then reports is in the
    fixture, not the code.
    """
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _ok_body() -> dict:
    """The reply verification needs: JSON it can parse."""
    return {"choices": [{"message": {"content": '{"ok": true}'}}]}


class TestStreamingTransport:
    """`ai_stream` changes the wire, not the answer.

    The real `OpenAiCompatibleClient` is used here (no client factory) because
    what is under test is the HTTP shape: an SSE body has to be reassembled into
    the same text a buffered body would have produced, and a provider that
    ignores `stream` and answers JSON must still be readable.
    """

    @pytest.mark.asyncio
    async def test_an_sse_body_is_reassembled(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen.append(payload)
            if not payload.get("stream"):
                # The connectivity check the chain insists on, answered the
                # ordinary way so the model may enter the chain at all.
                return httpx.Response(200, json=_ok_body())
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_body(
                    {"choices": [{"delta": {"role": "assistant"}}]},
                    {"choices": [{"delta": {"content": '{"directory":'}}]},
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": ' "a", "filename": "b"}'
                                }
                            }
                        ]
                    },
                ),
            )

        service = AiProviderService(
            database,
            settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await _ready_chain(service)
        await settings.save_ai_stream(True)
        seen.clear()

        answer = await service.complete([{"role": "user", "content": "hi"}])
        assert answer.text == '{"directory": "a", "filename": "b"}'
        assert seen[0]["stream"] is True

    @pytest.mark.asyncio
    async def test_a_provider_that_ignores_the_flag_still_answers(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            # Stream requested, ordinary body returned: the flag was ignored.
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"directory": "", "filename": "b"}'}}
                    ]
                },
            )

        service = AiProviderService(
            database,
            settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await _ready_chain(service)
        await settings.save_ai_stream(True)

        answer = await service.complete([{"role": "user", "content": "hi"}])
        assert json.loads(answer.text)["filename"] == "b"

    @pytest.mark.asyncio
    async def test_the_default_is_not_to_stream(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_ok_body())

        service = AiProviderService(
            database,
            settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await _ready_chain(service)
        seen.clear()

        await service.complete([{"role": "user", "content": "hi"}])
        assert "stream" not in seen[0]

    @pytest.mark.asyncio
    async def test_an_empty_stream_is_a_bad_response(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if not payload.get("stream"):
                return httpx.Response(200, json=_ok_body())
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_body(),
            )

        service = AiProviderService(
            database,
            settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await _ready_chain(service)
        await settings.save_ai_stream(True)

        # The chain walks to the last model and then reports every failure at
        # once: the unreadable body is the reason, and the code says the chain
        # came up empty of answers rather than that HTTP broke.
        with pytest.raises(AiError) as caught:
            await service.complete([{"role": "user", "content": "hi"}])
        assert caught.value.code == "AI_CHAIN_UNAVAILABLE"
        assert "没有可读的文本内容" in caught.value.public_message


# ---------------------------------------------------------------------------
#  批量录入：一次粘贴多把 Key、一次勾选多个模型
# ---------------------------------------------------------------------------


class TestBatchEntry:
    @pytest.mark.asyncio
    async def test_a_pasted_block_of_keys_becomes_several_rows(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        keys = await service.add_keys(
            provider.provider_id,
            """
            # 这是注释行，会被忽略

            主力:sk-one
            备用:sk-two
            sk-three-without-a-label
            """,
        )
        assert [key.label for key in keys] == ["主力", "备用", ""]
        # 三把都能解密回原文：批量入库没有把某一行的密文写串。
        plaintexts = [await service._key_plaintext(key.key_id) for key in keys]
        assert plaintexts == ["sk-one", "sk-two", "sk-three-without-a-label"]

    @pytest.mark.asyncio
    async def test_an_empty_key_block_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        with pytest.raises(AiError) as caught:
            await service.add_keys(provider.provider_id, "\n  \n")
        assert caught.value.code == AI_KEY_INVALID

    @pytest.mark.asyncio
    async def test_a_model_checklist_deduplicates_and_keeps_existing_rows(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        first = await service.add_models(provider.provider_id, ["a", "b"])
        again = await service.add_models(provider.provider_id, ["b", "c", "b"])
        assert [model.name for model in again] == ["b", "c"]
        assert again[0].model_id == first[1].model_id
        assert [model.name for model in await service.models(provider.provider_id)] == [
            "a",
            "b",
            "c",
        ]

    @pytest.mark.asyncio
    async def test_an_empty_model_checklist_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        with pytest.raises(AiError) as caught:
            await service.add_models(provider.provider_id, ["", "  "])
        assert caught.value.code == AI_MODEL_INVALID


# ---------------------------------------------------------------------------
#  请求参数：默认不发，逐模型可覆盖
# ---------------------------------------------------------------------------


def _capture_transport(captured: list[dict]) -> httpx.MockTransport:
    """An httpx transport that records the request body and answers 200."""

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
        )

    return httpx.MockTransport(handle)


class TestRequestParams:
    @pytest.mark.asyncio
    async def test_the_default_body_is_model_and_messages_only(self) -> None:
        """「不填就不发」：默认请求体里没有 temperature / max_tokens。

        这条是这次重写的关键回归：写死这两个字段会让 OpenAI 推理模型（不接受
        temperature、要 max_completion_tokens）永远验证失败。
        """
        captured: list[dict] = []
        async with httpx.AsyncClient(transport=_capture_transport(captured)) as http:
            client = OpenAiCompatibleClient(
                http, base_url="http://x/v1", api_key="k", model="m"
            )
            await client.complete([{"role": "user", "content": "hi"}])
        assert captured == [
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        ]

    @pytest.mark.asyncio
    async def test_a_model_override_reaches_the_body(self) -> None:
        captured: list[dict] = []
        params = AiRequestParams(
            temperature=0.3, extra_body={"max_completion_tokens": 512}
        )
        async with httpx.AsyncClient(transport=_capture_transport(captured)) as http:
            client = OpenAiCompatibleClient(
                http, base_url="http://x/v1", api_key="k", model="m"
            )
            await client.complete(
                [{"role": "user", "content": "hi"}], params=params
            )
        assert captured[0]["temperature"] == 0.3
        assert captured[0]["max_completion_tokens"] == 512
        assert "max_tokens" not in captured[0]

    @pytest.mark.asyncio
    async def test_extra_headers_are_sent_but_never_override_the_credential(
        self,
    ) -> None:
        seen: list[httpx.Headers] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "{}"}}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = OpenAiCompatibleClient(
                http,
                base_url="http://x/v1",
                api_key="secret",
                model="m",
                extra_headers={"X-Org": "abc"},
            )
            await client.complete([{"role": "user", "content": "hi"}])
        assert seen[0]["x-org"] == "abc"
        assert seen[0]["authorization"] == "Bearer secret"

    @pytest.mark.asyncio
    async def test_a_provider_default_and_a_model_override_merge(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(
            _provider_values(default_params='{"temperature": 0.5, "top_p": 0.9}')
        )
        await service.add_key(provider.provider_id, label="", api_key="k")
        model = await service.add_model(provider.provider_id, "m")
        await service.save_model_params(model.model_id, '{"temperature": 0.1}')
        model = await service.model(model.model_id)
        merged = provider.default_params.merged(model.params)
        assert merged.temperature == 0.1
        assert merged.extra_body == {"top_p": 0.9}

    @pytest.mark.asyncio
    async def test_the_chain_entry_carries_the_merged_params(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _ready_chain(service)
        await service.save_model_params(model_ids[0], '{"max_tokens": 64}')
        chain = await service.chain()
        assert chain[0].request_params.max_tokens == 64

    @pytest.mark.asyncio
    async def test_a_params_box_that_is_not_json_is_refused(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.save_provider(_provider_values(default_params="nope"))
        assert caught.value.code == AI_PARAMS_INVALID

    @pytest.mark.asyncio
    async def test_a_credential_in_the_headers_box_is_refused(
        self, tmp_path: Path
    ) -> None:
        """请求头里的 Authorization 会绕过加密的 Key 列表，明文入库。"""
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        with pytest.raises(AiError) as caught:
            await service.save_provider(
                _provider_values(custom_headers='{"Authorization": "Bearer x"}')
            )
        assert caught.value.code == AI_PARAMS_INVALID

    @pytest.mark.asyncio
    async def test_a_60000_token_cap_is_accepted_as_an_extra_field(self) -> None:
        """运营者可以按模型写任意字段（如 max_completion_tokens）。"""
        params = AiRequestParams(extra_body={"max_completion_tokens": 60000})
        assert params.extra_body["max_completion_tokens"] == 60000


class TestParamRejection:
    def test_a_400_that_names_a_parameter_gets_its_own_code(self) -> None:
        error = _classify_status(
            400, "Unsupported parameter: 'temperature' is not supported with this model."
        )
        assert error.code == AI_PARAM_REJECTED
        assert "参数" in error.public_message

    def test_a_400_without_a_readable_body_is_not_blamed_on_parameters(self) -> None:
        assert _classify_status(400, "").code == AI_REQUEST_REJECTED


class TestVendorErrorInSuccessBody:
    """MiniMax answers 200 with `base_resp.status_code`, not an HTTP status.

    The operator hit exactly this: the OpenAI-compatible route returns 401 with
    「login fail: Please carry the API secret key in the 'Authorization' field of
    the request header (1004)」, and the native route returns the same sentence
    inside a 200. Both must land on the same code -- a credential fault -- or the
    key never rotates and the page says 「没有 choices」 instead of the vendor's
    own sentence.
    """

    BODY = {
        "base_resp": {
            "status_code": 1004,
            "status_msg": (
                "login fail: Please carry the API secret key in the "
                "'Authorization' field of the request header"
            ),
        }
    }

    def test_an_auth_code_is_a_key_fault(self) -> None:
        error = _error_envelope(self.BODY)
        assert error is not None
        assert error.code == AI_AUTH
        assert error.key_fault is True
        assert "login fail" in error.public_message

    def test_a_zero_code_is_not_an_error(self) -> None:
        assert _error_envelope({"base_resp": {"status_code": 0}}) is None

    def test_a_body_without_the_envelope_is_left_to_the_reader(self) -> None:
        assert _error_envelope({"choices": [{"message": {"content": "x"}}]}) is None
        assert _error_envelope([1, 2, 3]) is None

    def test_an_unknown_code_is_not_guessed_at(self) -> None:
        error = _error_envelope({"base_resp": {"status_code": 4242}})
        assert error is not None and error.code == AI_REQUEST_REJECTED

    def test_a_rate_code_rotates_the_key(self) -> None:
        error = _error_envelope({"base_resp": {"status_code": 1002}})
        assert error is not None and error.code == AI_RATE_LIMIT

    @pytest.mark.asyncio
    async def test_the_client_raises_instead_of_misreading_the_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self.BODY)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = OpenAiCompatibleClient(
                http, base_url="http://local/v1", api_key="k", model="m"
            )
            with pytest.raises(AiClientError) as caught:
                await client.complete([{"role": "user", "content": "hi"}])
        assert caught.value.code == AI_AUTH
        assert "login fail" in caught.value.public_message


class TestReasoningOutput:
    def test_a_think_block_is_stripped_before_the_json_is_found(self) -> None:
        text = _content_of(
            {
                "choices": [
                    {
                        "message": {
                            "content": "<think>{not json}</think>{\"ok\": true}"
                        }
                    }
                ]
            }
        )
        assert text == '{"ok": true}'
        assert extract_json_object(text) == {"ok": True}

    def test_an_answered_reasoning_field_is_ignored(self) -> None:
        text = _content_of(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"ok": true}',
                            "reasoning_content": "thinking about {braces}",
                        }
                    }
                ]
            }
        )
        assert text == '{"ok": true}'


class TestVerifyAll:
    @pytest.mark.asyncio
    async def test_only_enabled_models_are_tested(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider_id, _, model_ids = await _catalogue(
            service, model_names=("a", "b")
        )
        await service.set_model_enabled(model_ids[1], False)
        results = await service.verify_all(provider_id)
        assert len(results) == 1
        assert results[0].ok

    @pytest.mark.asyncio
    async def test_a_provider_with_nothing_enabled_reports_nothing(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider_id, _, _ = await _catalogue(service)
        assert await service.verify_all(provider_id + 100) == ()
