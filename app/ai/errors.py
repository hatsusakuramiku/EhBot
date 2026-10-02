"""The AI layer's refusal type.

One exception with a stable machine code and a sentence written for the
operator, matching `ArchiveSettingsError` / `ArchivedWorkError`. A caller that
must distinguish 「这把 Key 坏了，换下一把」 from 「这个模型不行，回落到下一个」 reads `code`;
a caller that only reports reads `public_message`.
"""

from __future__ import annotations

#: Configuration refusals. Each is a sentence the operator can act on from the
#: settings page, so the code exists for the tests and the log line rather than
#: for a second message.
AI_PROVIDER_INVALID = "AI_PROVIDER_INVALID"
AI_PROVIDER_NAME_TAKEN = "AI_PROVIDER_NAME_TAKEN"
AI_PROVIDER_NOT_FOUND = "AI_PROVIDER_NOT_FOUND"
AI_KEY_INVALID = "AI_KEY_INVALID"
AI_MODEL_INVALID = "AI_MODEL_INVALID"
AI_MODEL_NOT_FOUND = "AI_MODEL_NOT_FOUND"
#: Chain-entry refusals that R31 no longer raises. The old save-time gate made
#: an unverified, disabled or key-less model impossible to configure at all,
#: which also made whole classes of endpoints (reasoning models, gateways that
#: 400 on any unknown field) unconfigurable; 「能不能用」 is now answered by the
#: 测试 button, so these three stay only so old logs and stored codes still read
#: the same.
AI_MODEL_UNVERIFIED = "AI_MODEL_UNVERIFIED"
AI_MODEL_DISABLED = "AI_MODEL_DISABLED"
AI_PROVIDER_DISABLED = "AI_PROVIDER_DISABLED"
#: The params box on a provider or a model is not a JSON object we can send.
AI_PARAMS_INVALID = "AI_PARAMS_INVALID"
#: The provider answered 400 with a complaint about a *parameter* (temperature,
#: max_tokens, an unknown field). Distinct from 「配置写错了」 because the fix is
#: 「到这个模型的高级参数里关掉/换掉它」, which is a different page and a different
#: person's guess than 「地址或 Key 填错了」.
AI_PARAM_REJECTED = "AI_PARAM_REJECTED"
AI_NO_KEY = "AI_NO_KEY"
AI_CHAIN_EMPTY = "AI_CHAIN_EMPTY"
AI_CHAIN_DUPLICATE = "AI_CHAIN_DUPLICATE"
AI_CHAIN_ENTRY_MISSING = "AI_CHAIN_ENTRY_MISSING"
#: A 200 from the verification request whose body was not the JSON we asked for.
AI_VERIFY_OK = "AI_VERIFY_OK"
#: The model answered, and the answer cannot become a path: not JSON, a field of
#: the wrong type, an empty filename, a `..`/absolute segment, or a whole path
#: past the length ceiling. Refused rather than repaired because these are not
#: punctuation problems -- see proposal §7.
AI_PATH_INVALID = "AI_PATH_INVALID"
#: Operator-side reads only: there is no cached answer for this book yet, or the
#: one on file was produced from a different prompt / model chain / metadata.
#: Not a failure -- it is the prompt to ask, and asking happens in a packing job
#: rather than while somebody waits for a page to render.
AI_PATH_MISSING = "AI_PATH_MISSING"
#: The whole model chain failed. The caller turns this into 需干预, or into the
#: fallback template when the operator asked for that (proposal §7).
AI_PATH_UNAVAILABLE = "AI_PATH_UNAVAILABLE"
#: A candidate-admission answer that is not the `{"accept": bool, ...}` object we
#: asked for. Like `AI_PATH_INVALID`, this walks the chain: a model that answers
#: prose has not decided anything, and the next one gets the question.
AI_CANDIDATE_INVALID = "AI_CANDIDATE_INVALID"


class AiError(ValueError):
    """A configuration or request the AI layer will not carry out."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


__all__ = [
    "AI_CANDIDATE_INVALID",
    "AI_CHAIN_DUPLICATE",
    "AI_CHAIN_EMPTY",
    "AI_CHAIN_ENTRY_MISSING",
    "AI_KEY_INVALID",
    "AI_MODEL_DISABLED",
    "AI_MODEL_INVALID",
    "AI_MODEL_NOT_FOUND",
    "AI_MODEL_UNVERIFIED",
    "AI_NO_KEY",
    "AI_PARAMS_INVALID",
    "AI_PARAM_REJECTED",
    "AI_PATH_INVALID",
    "AI_PATH_MISSING",
    "AI_PATH_UNAVAILABLE",
    "AI_PROVIDER_DISABLED",
    "AI_PROVIDER_INVALID",
    "AI_PROVIDER_NAME_TAKEN",
    "AI_PROVIDER_NOT_FOUND",
    "AI_VERIFY_OK",
    "AiError",
]
