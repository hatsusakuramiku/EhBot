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
    AI_TIMEOUT,
    AiClientError,
)
from app.ai.errors import (
    AI_CHAIN_DUPLICATE,
    AI_CHAIN_EMPTY,
    AI_KEY_INVALID,
    AI_MODEL_DISABLED,
    AI_MODEL_UNVERIFIED,
    AI_NO_KEY,
    AI_PATH_UNAVAILABLE,
    AI_PROVIDER_DISABLED,
    AI_PROVIDER_INVALID,
    AI_PROVIDER_NAME_TAKEN,
    AiError,
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
        max_tokens: int,
        temperature: float,
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
        assert caught.value.code == AI_PATH_UNAVAILABLE
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
        assert caught.value.code == AI_PATH_UNAVAILABLE
        assert "first" in caught.value.public_message


# ---------------------------------------------------------------------------
#  Save-time refusal of the chain
# ---------------------------------------------------------------------------


class TestChainValidation:
    @pytest.mark.asyncio
    async def test_an_unverified_model_cannot_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        with pytest.raises(AiError) as caught:
            await service.save_chain(model_ids)
        assert caught.value.code == AI_MODEL_UNVERIFIED
        assert await service.chain() == ()

    @pytest.mark.asyncio
    async def test_a_disabled_model_cannot_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        await service.verify_model(model_ids[0])
        await service.set_model_enabled(model_ids[0], False)
        with pytest.raises(AiError) as caught:
            await service.save_chain(model_ids)
        assert caught.value.code == AI_MODEL_DISABLED

    @pytest.mark.asyncio
    async def test_a_provider_without_a_usable_key_cannot_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, key_ids, model_ids = await _catalogue(service)
        await service.verify_model(model_ids[0])
        await service.set_key_enabled(key_ids[0], False)
        with pytest.raises(AiError) as caught:
            await service.save_chain(model_ids)
        assert caught.value.code == AI_NO_KEY

    @pytest.mark.asyncio
    async def test_a_disabled_provider_cannot_enter_the_chain(
        self, tmp_path: Path
    ) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        provider = await service.save_provider(_provider_values())
        await service.add_key(provider.provider_id, label="", api_key="k")
        model = await service.add_model(provider.provider_id, "m")
        await service.verify_model(model.model_id)
        await service.save_provider(
            _provider_values(provider_id=provider.provider_id, enabled=False)
        )
        with pytest.raises(AiError) as caught:
            await service.save_chain([model.model_id])
        assert caught.value.code == AI_PROVIDER_DISABLED

    @pytest.mark.asyncio
    async def test_a_duplicate_entry_is_refused(self, tmp_path: Path) -> None:
        database, settings = await _seed(tmp_path)
        service = _service(database, settings)
        _, _, model_ids = await _catalogue(service)
        await service.verify_model(model_ids[0])
        with pytest.raises(AiError) as caught:
            await service.save_chain([model_ids[0], model_ids[0]])
        assert caught.value.code == AI_CHAIN_DUPLICATE

    @pytest.mark.asyncio
    async def test_reordering_and_removing_go_through_the_validator(
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
        assert caught.value.code == "AI_PATH_UNAVAILABLE"
        assert "没有可读的文本内容" in caught.value.public_message
