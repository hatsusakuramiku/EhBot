"""Rows and vocabulary for the AI provider layer.

The dataclasses mirror the five tables in migration 018. `AiProviderKey` carries
no plaintext and no fragment of it: a settings page shows the label and the
rotation state, never a credential, which is the same line the archive password
vault draws.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


#: Protocol adapters this build speaks. One today; the column and the vocabulary
#: exist so a second adapter (a vendor whose API is not OpenAI-shaped) is a value
#: plus a client class, not a schema change.
PROVIDER_CODE_OPENAI = "openai"

#: Which feature a chain belongs to. One global default chain (managed next to
#: the providers) and one optional per-feature override each: the archive-path
#: page and the AI candidate gate either inherit the default or carry their own
#: list. A str column rather than a second table because 「主力 + 备用」 is the
#: same shape everywhere and the only difference is who reads it.
CHAIN_SCOPE_DEFAULT = "default"
CHAIN_SCOPE_ARCHIVE_PATH = "archive_path"
CHAIN_SCOPE_CANDIDATE = "candidate_admission"
CHAIN_SCOPES: tuple[str, ...] = (
    CHAIN_SCOPE_DEFAULT,
    CHAIN_SCOPE_ARCHIVE_PATH,
    CHAIN_SCOPE_CANDIDATE,
)

#: Human names for the scopes, for the pages and the failure text that have to
#: say 「哪一个功能」 without the reader knowing the raw code.
CHAIN_SCOPE_LABELS: dict[str, str] = {
    CHAIN_SCOPE_DEFAULT: "全局默认",
    CHAIN_SCOPE_ARCHIVE_PATH: "归档路径",
    CHAIN_SCOPE_CANDIDATE: "AI 候选判定",
}

#: 「这张列表从哪来」: follow the global default, or use this feature's own.
#: Lives here rather than in the archive settings service because every scoped
#: feature speaks it, and a candidate gate importing the archive service to say
#: 「custom」 would be the odd dependency in the other direction.
MODEL_SOURCE_DEFAULT = "default"
MODEL_SOURCE_CUSTOM = "custom"
MODEL_SOURCES: tuple[str, ...] = (MODEL_SOURCE_DEFAULT, MODEL_SOURCE_CUSTOM)

SUPPORTED_PROVIDER_CODES: tuple[str, ...] = (PROVIDER_CODE_OPENAI,)

PROVIDER_CODE_LABELS: dict[str, str] = {
    PROVIDER_CODE_OPENAI: "OpenAI 兼容",
}

DEFAULT_PROVIDER_CODE = PROVIDER_CODE_OPENAI
DEFAULT_TIMEOUT_SECONDS = 30
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 300
DEFAULT_MAX_RETRIES = 1
MAX_RETRIES = 3

#: How long a key that returned 401/403 (rejected) or 429 (rate limited) is
#: skipped. Long enough that a burst of failures does not keep retrying the same
#: rejected key, short enough that fixing a key upstream is noticed within a
#: coffee break.
KEY_COOLDOWN_MINUTES = 10

#: The cap on how many work titles one request may carry. The prompt only ever
#: needs a handful, and an unbounded list is how a single call turns into a
#: four-figure bill.
MAX_TAGS_IN_PROMPT = 60

#: Ceiling on the model's own answer, so a runaway completion cannot cost more
#: than the decision is worth.
MAX_OUTPUT_TOKENS = 900
DEFAULT_TEMPERATURE = 0.2

#: Field caps the settings forms and the validator agree on. A name longer than
#: this is either a paste accident or an attempt to make the dropdown unusable;
#: refusing it at save time is cheaper than rendering it.
MAX_PROVIDER_NAME_LENGTH = 80
MAX_MODEL_NAME_LENGTH = 120
MAX_KEY_LABEL_LENGTH = 60


@dataclass(frozen=True, slots=True)
class AiRequestParams:
    """The knobs one chat request may carry, per provider and per model.

    Every field is optional and **absent means 「do not send it」**. That is the
    whole point: some models reject parameters their neighbours accept (OpenAI's
    reasoning models refuse `temperature` and want `max_completion_tokens`), and
    a client that always sends a fixed body cannot talk to them at all. So the
    default request is `{"model": ..., "messages": ...}` and nothing else, and an
    operator opts a provider or a single model into whatever it needs -- with
    `extra_body` as the escape hatch for vendor-specific fields.
    """

    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return (
            self.temperature is None
            and self.max_tokens is None
            and not self.extra_body
        )

    def merged(self, override: "AiRequestParams") -> "AiRequestParams":
        """This layer with `override` applied; unset fields keep this value."""
        return AiRequestParams(
            temperature=(
                override.temperature
                if override.temperature is not None
                else self.temperature
            ),
            max_tokens=(
                override.max_tokens
                if override.max_tokens is not None
                else self.max_tokens
            ),
            extra_body={**self.extra_body, **override.extra_body},
        )


def parse_request_params(raw: object) -> AiRequestParams:
    """Read the JSON an operator typed into a params box.

    `None`/empty means 「no parameters」. Anything that is not a JSON object, or
    carries a field of the wrong type, raises `ValueError` -- the form and the
    migration both want a refusal with a reason, not a silent drop.
    """
    if raw is None:
        return AiRequestParams()
    if isinstance(raw, AiRequestParams):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return AiRequestParams()
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            raise ValueError("参数必须是合法的 JSON 对象") from exc
    else:
        decoded = raw
    if not isinstance(decoded, dict):
        raise ValueError("参数必须是一个 JSON 对象")
    # Anything that is not one of the two named knobs rides in `extra_body`:
    # 「model 不接受 temperature 就换成 max_completion_tokens」 must be one line in
    # a text box, not a nested object an operator has to know the shape of.
    reserved = {"model", "messages", "stream"}
    temperature = decoded.get("temperature")
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError) as exc:
            raise ValueError("temperature 必须是数字") from exc
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature 必须在 0 到 2 之间")
    max_tokens = decoded.get("max_tokens")
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            if not (isinstance(max_tokens, str) and max_tokens.strip().isdigit()):
                raise ValueError("max_tokens 必须是正整数")
            max_tokens = int(str(max_tokens).strip())
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须是正整数")
    extra_body = decoded.get("extra_body") or {}
    if not isinstance(extra_body, dict):
        raise ValueError("extra_body 必须是 JSON 对象")
    for key, value in decoded.items():
        if key in ("temperature", "max_tokens", "extra_body"):
            continue
        if key in reserved:
            raise ValueError(f"{key} 由本服务自行填写，不能在参数里覆盖")
        extra_body[key] = value
    for key in reserved & set(extra_body):
        raise ValueError(f"{key} 由本服务自行填写，不能在参数里覆盖")
    return AiRequestParams(
        temperature=temperature, max_tokens=max_tokens, extra_body=dict(extra_body)
    )


@dataclass(frozen=True, slots=True)
class AiProvider:
    """One configured vendor endpoint (AstrBot's 「供应商来源」)."""

    provider_id: int
    name: str
    code: str
    base_url: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    enabled: bool = True
    #: Extra request headers for a gateway that wants its own field. A proxy is
    #: deliberately *not* here: `httpx` honours `HTTP(S)_PROXY` from the
    #: environment, and one proxy setting per provider would be a knob whose
    #: only correct value is the one the deployment already sets.
    custom_headers: dict[str, Any] = field(default_factory=dict)
    default_params: AiRequestParams = field(default_factory=AiRequestParams)


@dataclass(frozen=True, slots=True)
class AiProviderKey:
    """One API key on a provider. `cipher` never leaves the service."""

    key_id: int
    provider_id: int
    label: str
    enabled: bool
    failures: int
    cooldown_until: str | None
    last_used_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class AiProviderModel:
    """One model name offered by a provider, with its verification state.

    `last_verify_ok is None` means 「从未验证」, which is a different fact from
    「验证失败」 -- the first is a step not taken yet, the second is a step that
    was taken and failed, and the settings page says them differently.
    """

    model_id: int
    provider_id: int
    name: str
    enabled: bool = True
    last_verified_at: str | None = None
    last_verify_ok: bool | None = None
    last_verify_error: str | None = None
    params: AiRequestParams = field(default_factory=AiRequestParams)

    @property
    def verified(self) -> bool:
        """Whether a connectivity check has passed for this model.

        Deliberately not 「not failed」: an entry that was never tested has no
        business in the chain, which is what makes 「保存即验证」 enforceable.
        """
        return self.last_verify_ok is True


@dataclass(frozen=True, slots=True)
class AiModelChainEntry:
    """One position in the primary/fallback chain.

    `position` 0 is the primary model; the rest are the fallbacks, in order.
    The pair (provider, model) is what is configured, so one provider can appear
    at two positions with different models and two providers can hold the same
    model name.
    """

    position: int
    provider: AiProvider
    model: AiProviderModel
    scope: str = CHAIN_SCOPE_DEFAULT

    @property
    def is_primary(self) -> bool:
        return self.position == 0

    @property
    def request_params(self) -> AiRequestParams:
        """What this entry actually sends: provider default, then model override."""
        return self.provider.default_params.merged(self.model.params)


@dataclass(frozen=True, slots=True)
class AiAnswer:
    """One completion, with the identity of whoever produced it.

    The text alone would be enough to decide a path, but the cache row and the
    log line both need to name the model that answered -- 「这本书为什么在这」
    includes 「谁定的」. `key_id` is an id, never the credential.
    """

    text: str
    provider_id: int
    provider_name: str
    model_name: str
    key_id: int


@dataclass(frozen=True, slots=True)
class AiPathSuggestion:
    """One cached path answer, exactly as `ai_path_suggestions` holds it.

    This is the model layer's record and nothing more: it says 「this input, this
    prompt and this chain produced this relative path」. It is deliberately not a
    second truth about where the book lives -- that is the pin, and the pin can
    disagree with this row (an operator renamed the book). The row exists so the
    detail page, the packer and a later re-archive sweep all read one answer
    instead of asking a model that is not deterministic.

    `relative_path` already carries the `.cbz` suffix; `filename` does not, so a
    form and a log line each get the shape they need without re-deriving one from
    the other.
    """

    candidate_id: int
    fingerprint: str
    prompt_hash: str
    relative_path: str
    directory: str
    filename: str
    provider_id: int | None
    model_name: str
    attempts: int = 1
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class AiPathOutcome:
    """One decision, plus where it came from.

    `from_cache` is not an optimisation detail: it is the difference between
    「this book has a recorded answer」 and 「a model was asked just now」, which is
    what the structured log line and the tests both care about.
    """

    suggestion: AiPathSuggestion
    from_cache: bool


@dataclass(frozen=True, slots=True)
class AiVerification:
    """The outcome of one connectivity check, as the page reports it."""

    ok: bool
    code: str
    message: str


__all__ = [
    "CHAIN_SCOPES",
    "CHAIN_SCOPE_ARCHIVE_PATH",
    "CHAIN_SCOPE_CANDIDATE",
    "CHAIN_SCOPE_DEFAULT",
    "CHAIN_SCOPE_LABELS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PROVIDER_CODE",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_SECONDS",
    "KEY_COOLDOWN_MINUTES",
    "MAX_KEY_LABEL_LENGTH",
    "MAX_MODEL_NAME_LENGTH",
    "MAX_OUTPUT_TOKENS",
    "MAX_PROVIDER_NAME_LENGTH",
    "MAX_RETRIES",
    "MAX_TAGS_IN_PROMPT",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "MODEL_SOURCES",
    "MODEL_SOURCE_CUSTOM",
    "MODEL_SOURCE_DEFAULT",
    "PROVIDER_CODE_LABELS",
    "PROVIDER_CODE_OPENAI",
    "SUPPORTED_PROVIDER_CODES",
    "AiAnswer",
    "AiModelChainEntry",
    "AiPathOutcome",
    "AiPathSuggestion",
    "AiProvider",
    "AiProviderKey",
    "AiProviderModel",
    "AiRequestParams",
    "AiVerification",
    "parse_request_params",
]
