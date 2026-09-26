"""The provider catalogue, the key rotation and the model chain.

This is the layer the settings page and (in R29) the path decision both talk to.
It owns three things:

* what is configured -- providers, their keys, their model names, the ordered
  primary/fallback chain -- with the validation that keeps a half-finished
  configuration out of the database;
* *which* key and *which* model answers a request, rotating on a 401/429 and
  walking the fallbacks on a timeout or a 5xx;
* the connectivity check the settings page insists on before a model may enter
  the chain.

It does not own the prompt, the metadata payload or the path parsing; those
belong to the caller, so a change to what we ask the model never has to touch
the rotation rules, and the rotation rules can be tested with a fake client and
no metadata at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

import httpx

from app.ai.client import (
    AI_AUTH,
    AI_BAD_RESPONSE,
    AI_RATE_LIMIT,
    AI_REQUEST_REJECTED,
    AI_UNREACHABLE,
    AiClientError,
    OpenAiCompatibleClient,
)
from app.ai.errors import (
    AI_CHAIN_DUPLICATE,
    AI_CHAIN_EMPTY,
    AI_CHAIN_ENTRY_MISSING,
    AI_KEY_INVALID,
    AI_MODEL_DISABLED,
    AI_MODEL_INVALID,
    AI_MODEL_NOT_FOUND,
    AI_MODEL_UNVERIFIED,
    AI_NO_KEY,
    AI_PATH_UNAVAILABLE,
    AI_PROVIDER_DISABLED,
    AI_PROVIDER_INVALID,
    AI_PROVIDER_NAME_TAKEN,
    AI_PROVIDER_NOT_FOUND,
    AI_VERIFY_OK,
    AiError,
)
from app.ai.models import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_PROVIDER_CODE,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_SECONDS,
    KEY_COOLDOWN_MINUTES,
    MAX_KEY_LABEL_LENGTH,
    MAX_MODEL_NAME_LENGTH,
    MAX_OUTPUT_TOKENS,
    MAX_PROVIDER_NAME_LENGTH,
    MAX_RETRIES,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    PROVIDER_CODE_LABELS,
    SUPPORTED_PROVIDER_CODES,
    AiAnswer,
    AiModelChainEntry,
    AiProvider,
    AiProviderKey,
    AiProviderModel,
    AiVerification,
)
from app.archive.vault import VaultError, decrypt_password, encrypt_password


#: The verification request. Deliberately tiny: the point is to prove the
#: address, the key, the model name and structured output all work, and a
#: longer prompt would only make the check cost more than the decision.
VERIFY_MESSAGES: tuple[dict[str, str], ...] = (
    {
        "role": "system",
        "content": (
            "You are a connectivity probe. Answer with JSON only, no prose."
        ),
    },
    {
        "role": "user",
        "content": 'Reply with exactly {"ok": true} and nothing else.',
    },
)
VERIFY_MAX_TOKENS = 64

#: How many model names one 「拉取模型」 call will offer. A provider that lists
#: hundreds of them would otherwise turn the page into a wall of checkboxes.
MAX_DISCOVERED_MODELS = 200


def extract_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in an assistant message, or `AI_BAD_RESPONSE`.

    Models wrap JSON in a fenced block, preface it with 「好的」 or add a
    trailing sentence often enough that refusing anything but a bare object
    would fail a request that plainly answered. So the braces are located and
    the slice between them is parsed; that is lenient about padding and still
    strict about the payload being an object.
    """
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise AiError(AI_BAD_RESPONSE, "AI 输出里没有 JSON 对象")
    try:
        payload = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise AiError(AI_BAD_RESPONSE, "AI 输出的 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise AiError(AI_BAD_RESPONSE, "AI 输出的 JSON 不是对象")
    return payload


class AiProviderService:
    """Everything the AI path feature knows about its own configuration."""

    def __init__(
        self,
        database: Any,
        archive_settings: Any,
        *,
        http_client: httpx.AsyncClient,
        client_factory: Callable[..., OpenAiCompatibleClient] | None = None,
    ) -> None:
        self._database = database
        self._archive_settings = archive_settings
        self._http = http_client
        self._client_factory = client_factory or OpenAiCompatibleClient
        # Rotation cursors, keyed by provider. Deliberately in memory: the
        # proposal only asks for round-robin, not for a durable count, and a
        # restart starting again at the first key is harmless because a key in
        # cooldown is skipped from the database, not from this dict.
        self._cursors: dict[int, int] = {}

    # ------------------------------------------------------------------
    #  Providers
    # ------------------------------------------------------------------
    async def providers(self, *, enabled_only: bool = False) -> tuple[AiProvider, ...]:
        return await self._database.list_ai_providers(enabled_only=enabled_only)

    async def provider(self, provider_id: int) -> AiProvider | None:
        return await self._database.get_ai_provider(provider_id)

    async def _require_provider(self, provider_id: int) -> AiProvider:
        provider = await self._database.get_ai_provider(provider_id)
        if provider is None:
            raise AiError(
                AI_PROVIDER_NOT_FOUND, f"AI 供应商 {provider_id} 不存在"
            )
        return provider

    async def save_provider(self, values: dict[str, Any]) -> AiProvider:
        """Insert or update one provider after validating the form.

        The address check is the same one the torrent client save makes and for
        the same reason: a typo found here is a line on a form, the same typo
        found at pack time is a book stuck in 需干预.
        """
        raw_id = str(values.get("provider_id") or "").strip()
        provider_id = int(raw_id) if raw_id.isdigit() else None
        name = str(values.get("name") or "").strip()
        if not name:
            raise AiError(AI_PROVIDER_INVALID, "供应商名称不能为空")
        if len(name) > MAX_PROVIDER_NAME_LENGTH:
            raise AiError(
                AI_PROVIDER_INVALID,
                f"供应商名称不能超过 {MAX_PROVIDER_NAME_LENGTH} 个字符",
            )
        code = str(values.get("code") or DEFAULT_PROVIDER_CODE).strip()
        if code not in SUPPORTED_PROVIDER_CODES:
            raise AiError(
                AI_PROVIDER_INVALID,
                f"不支持的供应商编码：{code}（当前只支持 "
                + "、".join(
                    PROVIDER_CODE_LABELS.get(item, item)
                    for item in SUPPORTED_PROVIDER_CODES
                )
                + "）",
            )
        base_url = str(values.get("base_url") or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise AiError(
                AI_PROVIDER_INVALID,
                "基础 API 地址必须是以 http:// 或 https:// 开头的完整地址",
            )
        timeout_seconds = self._int_field(
            values.get("timeout_seconds"),
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=MIN_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
            label="超时",
        )
        max_retries = self._int_field(
            values.get("max_retries"),
            default=DEFAULT_MAX_RETRIES,
            minimum=0,
            maximum=MAX_RETRIES,
            label="重试次数",
        )
        try:
            return await self._database.save_ai_provider(
                provider_id=provider_id,
                name=name,
                code=code,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                enabled=bool(values.get("enabled")),
            )
        except LookupError as exc:
            raise AiError(AI_PROVIDER_NAME_TAKEN, f"供应商名称「{name}」已被占用") from exc

    @staticmethod
    def _int_field(
        raw: Any,
        *,
        default: int,
        minimum: int,
        maximum: int,
        label: str,
    ) -> int:
        text = str(raw or "").strip()
        if text == "":
            return default
        try:
            value = int(text)
        except ValueError as exc:
            raise AiError(AI_PROVIDER_INVALID, f"{label}必须是整数") from exc
        if value < minimum or value > maximum:
            raise AiError(
                AI_PROVIDER_INVALID,
                f"{label}必须在 {minimum} 到 {maximum} 之间",
            )
        return value

    async def delete_provider(self, provider_id: int) -> None:
        try:
            await self._database.delete_ai_provider(provider_id)
        except LookupError as exc:
            raise AiError(
                AI_PROVIDER_NOT_FOUND, f"AI 供应商 {provider_id} 不存在"
            ) from exc

    # ------------------------------------------------------------------
    #  Keys
    # ------------------------------------------------------------------
    async def keys(
        self, provider_id: int, *, usable_only: bool = False
    ) -> tuple[AiProviderKey, ...]:
        return await self._database.list_ai_provider_keys(
            provider_id, usable_only=usable_only
        )

    async def add_key(
        self, provider_id: int, *, label: str, api_key: str
    ) -> AiProviderKey:
        """Store one encrypted key. The plaintext is never read back here."""
        await self._require_provider(provider_id)
        secret = str(api_key or "").strip()
        if not secret:
            raise AiError(AI_KEY_INVALID, "API Key 不能为空")
        cleaned_label = str(label or "").strip()
        if len(cleaned_label) > MAX_KEY_LABEL_LENGTH:
            raise AiError(
                AI_KEY_INVALID,
                f"备注不能超过 {MAX_KEY_LABEL_LENGTH} 个字符",
            )
        master = await self._archive_settings.master_key()
        cipher = await asyncio.to_thread(encrypt_password, master, secret)
        return await self._database.add_ai_provider_key(
            provider_id, label=cleaned_label, cipher=cipher
        )

    async def set_key_enabled(self, key_id: int, enabled: bool) -> None:
        try:
            await self._database.set_ai_provider_key_enabled(key_id, enabled)
        except LookupError as exc:
            raise AiError(AI_KEY_INVALID, f"API Key {key_id} 不存在") from exc

    async def delete_key(self, key_id: int) -> None:
        try:
            await self._database.delete_ai_provider_key(key_id)
        except LookupError as exc:
            raise AiError(AI_KEY_INVALID, f"API Key {key_id} 不存在") from exc

    async def _key_plaintext(self, key_id: int) -> str | None:
        """Decrypt one key for one request, or None when it cannot be opened.

        A ciphertext the master key can no longer open is reported the same way
        a missing key is -- skipped -- because both mean 「这把用不了」 and the
        alternative is failing the whole request over one bad row.
        """
        cipher = await self._database.ai_provider_key_cipher(key_id)
        if not cipher:
            return None
        master = await self._archive_settings.master_key()
        try:
            return await asyncio.to_thread(decrypt_password, master, cipher)
        except VaultError:
            logging.getLogger(__name__).warning(
                "ai_provider_key_unreadable", extra={"error_code": "AI_KEY_INVALID"}
            )
            return None

    async def _ordered_keys(self, provider_id: int) -> list[AiProviderKey]:
        """Enabled, cooled-down keys starting at this provider's cursor.

        `usable_only` is the database's filter: a key inside its 401/429
        cooldown is not in the list at all, which is what makes a rotation
        survive a restart without re-trying a key that just failed.
        """
        keys = list(
            await self._database.list_ai_provider_keys(
                provider_id, usable_only=True
            )
        )
        if not keys:
            return []
        start = self._cursors.get(provider_id, 0) % len(keys)
        ordered = keys[start:] + keys[:start]
        self._cursors[provider_id] = (start + 1) % len(keys)
        return ordered

    # ------------------------------------------------------------------
    #  Models
    # ------------------------------------------------------------------
    async def models(self, provider_id: int) -> tuple[AiProviderModel, ...]:
        return await self._database.list_ai_provider_models(provider_id)

    async def model(self, model_id: int) -> AiProviderModel | None:
        return await self._database.get_ai_provider_model(model_id)

    async def add_model(self, provider_id: int, name: str) -> AiProviderModel:
        await self._require_provider(provider_id)
        cleaned = str(name or "").strip()
        if not cleaned:
            raise AiError(AI_MODEL_INVALID, "模型名称不能为空")
        if len(cleaned) > MAX_MODEL_NAME_LENGTH:
            raise AiError(
                AI_MODEL_INVALID,
                f"模型名称不能超过 {MAX_MODEL_NAME_LENGTH} 个字符",
            )
        return await self._database.add_ai_provider_model(provider_id, cleaned)

    async def set_model_enabled(self, model_id: int, enabled: bool) -> None:
        try:
            await self._database.set_ai_provider_model_enabled(model_id, enabled)
        except LookupError as exc:
            raise AiError(AI_MODEL_NOT_FOUND, f"模型 {model_id} 不存在") from exc

    async def delete_model(self, model_id: int) -> None:
        try:
            await self._database.delete_ai_provider_model(model_id)
        except LookupError as exc:
            raise AiError(AI_MODEL_NOT_FOUND, f"模型 {model_id} 不存在") from exc

    async def list_remote_models(self, provider_id: int) -> tuple[str, ...]:
        """`GET /v1/models`, as names, for the 「拉取模型」 button.

        Not registration and not verification: a name that appears here still
        has to be added and then pass the chat check before it can enter the
        chain, because a listing endpoint says nothing about whether the model
        can answer.
        """
        provider = await self._require_provider(provider_id)
        keys = await self._ordered_keys(provider_id)
        if not keys:
            raise AiError(AI_NO_KEY, f"供应商「{provider.name}」没有可用的 API Key")
        last: AiError | None = None
        url = f"{provider.base_url}/models"
        for key in keys:
            plaintext = await self._key_plaintext(key.key_id)
            if not plaintext:
                continue
            try:
                response = await self._http.get(
                    url,
                    headers={"Authorization": f"Bearer {plaintext}"},
                    timeout=provider.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last = AiError(AI_UNREACHABLE, f"无法连接 AI 供应商：{type(exc).__name__}")
                continue
            if response.status_code in (401, 403):
                await self._database.record_ai_key_failure(
                    key.key_id, cooldown_minutes=KEY_COOLDOWN_MINUTES
                )
                last = AiError(AI_AUTH, "AI 供应商拒绝了这把 API Key")
                continue
            if response.status_code == 429:
                await self._database.record_ai_key_failure(
                    key.key_id, cooldown_minutes=KEY_COOLDOWN_MINUTES
                )
                last = AiError(AI_RATE_LIMIT, "AI 供应商限流或额度用尽")
                continue
            if response.status_code >= 400:
                last = AiError(
                    AI_REQUEST_REJECTED,
                    f"AI 供应商返回 HTTP {response.status_code}",
                )
                continue
            try:
                payload = response.json()
                entries = payload.get("data") if isinstance(payload, dict) else None
                names = tuple(
                    str(item["id"]).strip()
                    for item in entries
                    if isinstance(item, dict) and str(item.get("id", "")).strip()
                )
            except (ValueError, TypeError, KeyError, AttributeError):
                last = AiError(AI_BAD_RESPONSE, "AI 供应商的模型列表无法解析")
                continue
            await self._database.mark_ai_key_used(key.key_id)
            return names[:MAX_DISCOVERED_MODELS]
        raise last or AiError(AI_NO_KEY, f"供应商「{provider.name}」没有可用的 API Key")

    # ------------------------------------------------------------------
    #  Model chain
    # ------------------------------------------------------------------
    async def chain(self) -> tuple[AiModelChainEntry, ...]:
        return await self._database.list_ai_model_chain()

    async def save_chain(self, model_ids: Sequence[int]) -> tuple[AiModelChainEntry, ...]:
        """Replace the chain, refusing anything that cannot work.

        The refusal is the feature the operator asked for: 「配置时需要验证
        服务联通性」. A model that was never verified, was verified and failed,
        is switched off, or sits on a provider with no usable key is rejected
        here with the reason, rather than discovered when a book is packed.
        """
        cleaned: list[int] = []
        for raw in model_ids:
            model_id = int(raw)
            if model_id in cleaned:
                raise AiError(
                    AI_CHAIN_DUPLICATE, "模型链里同一个模型只能出现一次"
                )
            cleaned.append(model_id)
        for model_id in cleaned:
            model = await self._database.get_ai_provider_model(model_id)
            if model is None:
                raise AiError(AI_MODEL_NOT_FOUND, f"模型 {model_id} 不存在")
            if not model.enabled:
                raise AiError(
                    AI_MODEL_DISABLED,
                    f"模型「{model.name}」已停用，不能加入模型链",
                )
            if not model.verified:
                raise AiError(
                    AI_MODEL_UNVERIFIED,
                    f"模型「{model.name}」尚未通过联通性验证，不能加入模型链",
                )
            provider = await self._database.get_ai_provider(model.provider_id)
            if provider is None:
                raise AiError(AI_PROVIDER_NOT_FOUND, "模型所属的供应商不存在")
            if not provider.enabled:
                raise AiError(
                    AI_PROVIDER_DISABLED,
                    f"供应商「{provider.name}」已停用，其模型不能加入模型链",
                )
            if not await self._database.list_ai_provider_keys(
                provider.provider_id, usable_only=True
            ):
                raise AiError(
                    AI_NO_KEY,
                    f"供应商「{provider.name}」没有启用的 API Key",
                )
        return await self._database.save_ai_model_chain(cleaned)

    async def append_to_chain(self, model_id: int) -> tuple[AiModelChainEntry, ...]:
        ids = [entry.model.model_id for entry in await self.chain()]
        if model_id not in ids:
            ids.append(model_id)
        return await self.save_chain(ids)

    async def remove_from_chain(self, model_id: int) -> tuple[AiModelChainEntry, ...]:
        ids = [entry.model.model_id for entry in await self.chain()]
        if model_id not in ids:
            raise AiError(AI_CHAIN_ENTRY_MISSING, "该模型不在模型链里")
        return await self.save_chain([mid for mid in ids if mid != model_id])

    async def shift_chain(self, model_id: int, delta: int) -> tuple[AiModelChainEntry, ...]:
        """Move one entry by `delta` positions, clamped to the ends.

        Buttons rather than drag-and-drop: the repository's settings forms are
        plain HTML posts, and an ordered list is exactly the shape a form can
        carry without a second source of truth in the browser.
        """
        ids = [entry.model.model_id for entry in await self.chain()]
        if model_id not in ids:
            raise AiError(AI_CHAIN_ENTRY_MISSING, "该模型不在模型链里")
        index = ids.index(model_id)
        target = max(0, min(len(ids) - 1, index + delta))
        if target != index:
            ids.insert(target, ids.pop(index))
        return await self.save_chain(ids)

    # ------------------------------------------------------------------
    #  Verification
    # ------------------------------------------------------------------
    async def verify_model(self, model_id: int) -> AiVerification:
        """Prove one `(provider, model)` can answer, and record the result.

        Answers rather than raises: 「验证失败」 is an outcome the page reports
        next to the model, including for a request that never reached anyone
        (`AI_UNREACHABLE`) or that answered something unparseable
        (`AI_BAD_RESPONSE`). Both are stored, so the chain editor can refuse an
        entry whose last check failed without trying again from the page.
        """
        model = await self._database.get_ai_provider_model(model_id)
        if model is None:
            raise AiError(AI_MODEL_NOT_FOUND, f"模型 {model_id} 不存在")
        provider = await self._database.get_ai_provider(model.provider_id)
        if provider is None:
            raise AiError(AI_PROVIDER_NOT_FOUND, "模型所属的供应商不存在")
        try:
            answer = await self._ask(
                provider,
                model,
                VERIFY_MESSAGES,
                max_tokens=VERIFY_MAX_TOKENS,
                temperature=0.0,
                # A failed check must not park the key. Parking is a rotation
                # rule for the packing path -- 「别再撞同一把坏 Key」 -- but here the
                # operator is looking at the answer, and a cooldown would turn
                # the second click from 「鉴权失败」 into 「没有可用的 Key」, hiding
                # the reason they are trying to read.
                park_key_faults=False,
            )
        except AiError as exc:
            message = exc.public_message
            await self._database.mark_ai_model_verified(
                model_id, ok=False, error=f"{exc.code}: {message}"
            )
            return AiVerification(ok=False, code=exc.code, message=message)
        try:
            extract_json_object(answer.text)
        except AiError as exc:
            await self._database.mark_ai_model_verified(
                model_id, ok=False, error=f"{exc.code}: {exc.public_message}"
            )
            return AiVerification(ok=False, code=exc.code, message=exc.public_message)
        await self._database.mark_ai_model_verified(model_id, ok=True, error=None)
        return AiVerification(
            ok=True,
            code=AI_VERIFY_OK,
            message=f"验证通过（{answer.provider_name} / {answer.model_name}）",
        )

    # ------------------------------------------------------------------
    #  Running a request
    # ------------------------------------------------------------------
    async def stream_enabled(self) -> bool:
        """Whether path requests should use the streaming transport.

        Read from the path settings rather than passed in by every caller: it is
        one operator preference for one kind of request, and threading it from
        the web layer down to the HTTP client would put the same lookup in four
        places. The value is a transport detail only -- `_ask` ignores it when it
        builds the request body, and nothing about the answer changes.
        """
        reader = getattr(self._archive_settings, "ai_stream", None)
        if reader is None:
            return False
        return bool(await reader())

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = MAX_OUTPUT_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        validate: Callable[[str], object] | None = None,
        stream: bool | None = None,
    ) -> AiAnswer:
        """Ask the chain, primary first, and return the first usable answer.

        A chain entry that is switched off is skipped silently -- it is in the
        list because it was configured, and disabling it is how the operator
        takes it out of rotation without deleting it. Any failure at all moves
        to the next entry: the caller wants a path, not a diagnosis of the
        first model that stumbled.

        `validate` is the caller's own acceptance test -- for the path feature,
        「这段文字能解析成一条合法路径吗」. It is a parameter rather than a
        second loop in the caller because an answer that is valid HTTP but
        useless prose must walk the chain exactly like a timeout does: 「返回非法
        JSON 也算失败」 (proposal §7) means the *next model* gets the question,
        and only `complete` knows which model is next.
        """
        chain = await self.chain()
        if not chain:
            raise AiError(
                AI_CHAIN_EMPTY,
                "还没有配置 AI 模型链，请到「设置 → AI 供应商」添加并验证模型",
            )
        # Resolved once per call, not once per attempt: the answer must not
        # change shape halfway down the fallback chain, and one settings read per
        # book is one more than the question needs.
        if stream is None:
            stream = await self.stream_enabled()
        failures: list[str] = []
        for entry in chain:
            if not entry.provider.enabled or not entry.model.enabled:
                continue
            try:
                answer = await self._ask(
                    entry.provider,
                    entry.model,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                )
                if validate is not None:
                    validate(answer.text)
            except AiError as exc:
                failures.append(
                    f"{entry.provider.name}/{entry.model.name}：{exc.public_message}"
                )
                logging.getLogger(__name__).warning(
                    "ai_path_model_failed",
                    extra={
                        "error_code": exc.code,
                        "provider_id": entry.provider.provider_id,
                        "model": entry.model.name,
                    },
                )
                continue
            return answer
        if not failures:
            raise AiError(
                AI_CHAIN_EMPTY,
                "AI 模型链里的模型都已停用，请到「设置 → AI 供应商」启用至少一个",
            )
        raise AiError(
            AI_PATH_UNAVAILABLE, "所有 AI 模型都不可用：" + "；".join(failures[:3])
        )

    async def _ask(
        self,
        provider: AiProvider,
        model: AiProviderModel,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        stream: bool = False,
        park_key_faults: bool = True,
    ) -> AiAnswer:
        """One request through one provider/model, rotating its keys.

        A key that is refused (`key_fault`) is parked for its cooldown and the
        next key is tried; anything else is handed back to the caller, which
        decides whether that means 「下一把 Key」 (a 429 that was not flagged, a
        timeout) or 「下一个模型」.
        """
        keys = await self._ordered_keys(provider.provider_id)
        if not keys:
            raise AiError(
                AI_NO_KEY, f"供应商「{provider.name}」没有可用的 API Key"
            )
        last: AiError | None = None
        for key in keys:
            plaintext = await self._key_plaintext(key.key_id)
            if not plaintext:
                continue
            client = self._client_factory(
                self._http,
                base_url=provider.base_url,
                api_key=plaintext,
                model=model.name,
                timeout_seconds=provider.timeout_seconds,
                max_retries=provider.max_retries,
            )
            try:
                text = await client.complete(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                )
            except AiClientError as exc:
                last = exc
                if exc.key_fault and park_key_faults:
                    await self._database.record_ai_key_failure(
                        key.key_id, cooldown_minutes=KEY_COOLDOWN_MINUTES
                    )
                continue
            await self._database.mark_ai_key_used(key.key_id)
            return AiAnswer(
                text=text,
                provider_id=provider.provider_id,
                provider_name=provider.name,
                model_name=model.name,
                key_id=key.key_id,
            )
        raise last or AiError(
            AI_NO_KEY, f"供应商「{provider.name}」没有可用的 API Key"
        )


__all__ = [
    "MAX_DISCOVERED_MODELS",
    "VERIFY_MAX_TOKENS",
    "VERIFY_MESSAGES",
    "AiProviderService",
    "extract_json_object",
]
