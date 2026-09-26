"""Rows and vocabulary for the AI provider layer.

The dataclasses mirror the five tables in migration 018. `AiProviderKey` carries
no plaintext and no fragment of it: a settings page shows the label and the
rotation state, never a credential, which is the same line the archive password
vault draws.
"""

from __future__ import annotations

from dataclasses import dataclass


#: Protocol adapters this build speaks. One today; the column and the vocabulary
#: exist so a second adapter (a vendor whose API is not OpenAI-shaped) is a value
#: plus a client class, not a schema change.
PROVIDER_CODE_OPENAI = "openai"

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
class AiProvider:
    """One configured vendor endpoint."""

    provider_id: int
    name: str
    code: str
    base_url: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    enabled: bool = True


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

    @property
    def is_primary(self) -> bool:
        return self.position == 0


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
    "AiVerification",
]
