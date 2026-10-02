"""AI candidate admission: ask the model whether a message is a work at all.

The second, optional gate in front of the parse rules. It exists because the
channels a deployment watches post more than books -- chat, adverts, a stray
image -- and the cheap structural rules cannot tell 「一张图」 from 「一部作品的
预览」. When the operator turns it on, every message that got past the parser
becomes one short question to the global default model chain, and only `accept`
lets it through.

Three properties this module is built around:

* **Off unless asked for.** Both `ai_candidate_enabled` and a non-empty default
  chain are required before a single token is spent; with either missing the gate
  reports `skip` and the caller falls straight through to the parse rules. A
  deployment that never configures AI sees no extra requests and no failures.
* **Failure has an operator-chosen shape.** A chain that is configured but
  unusable on this request (`ai_candidate_fallback`) either rejects (the default
  -- fail closed, and say so in the log) or accepts (fail open for an operator
  who would rather review than lose a book).
* **The answer is parsed, never trusted.** `parse_candidate_decision` is the
  model's own acceptance test, handed to `complete` as `validate`, so prose or a
  missing field walks the fallback chain exactly like a timeout does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.ai.errors import AI_CANDIDATE_INVALID, AiError
from app.ai.models import CHAIN_SCOPE_DEFAULT
from app.ai.prompt import build_messages, candidate_payload
from app.ai.service import extract_json_object
from app.candidates.models import ParsedSourceMessage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Admission:
    """One gate's verdict.

    `verdict` is `accept`, `reject` or `skip`; `skip` means the gate is not in
    effect (disabled, or no provider configured) and the caller should continue
    to the parse rules. `override` is the operator's 「AI 通过即最终通过」 switch,
    carried here so the caller does not read the setting a second time.
    """

    verdict: str
    reason: str
    override: bool = False


def parse_candidate_decision(text: str) -> tuple[bool, str]:
    """The `{"accept": bool, "reason": str}` answer, or `AI_CANDIDATE_INVALID`.

    Strict about `accept` being a real boolean: a model that writes `"accept":
    "yes"` has not answered the question we asked, and guessing at it would be
    the one place this pipeline silently invents a decision.
    """
    payload = extract_json_object(text)
    value = payload.get("accept")
    if not isinstance(value, bool):
        raise AiError(
            AI_CANDIDATE_INVALID, "AI 判定输出缺少 accept 布尔字段"
        )
    reason = str(payload.get("reason") or "").strip()
    return value, reason


class CandidateAdmissionService:
    """Decide whether one message may become a candidate."""

    def __init__(self, ai_service: Any, settings: Any) -> None:
        self._ai = ai_service
        self._settings = settings

    async def decide(self, message: ParsedSourceMessage) -> Admission:
        """Ask the model about one message, or report that the gate is off."""
        config = await self._settings.candidate_admission()
        if not config["enabled"]:
            return Admission("skip", "AI 候选判定未开启")
        override = bool(config["override_parse_rules"])
        # A gate that is on but has nothing to ask cannot be allowed to fail
        # closed: 「配置了 AI 提供商并且手动开启」 is the condition for it to take
        # effect at all, and a deployment still setting up providers must not
        # stop ingesting. Logged so the operator can see why it did nothing.
        if not await self._ai.effective_chain(CHAIN_SCOPE_DEFAULT):
            logger.warning(
                "ai_candidate_admission_inactive",
                extra={"error_code": "AI_CANDIDATE_CHAIN_EMPTY"},
            )
            return Admission("skip", "尚未配置 AI 模型，AI 候选判定未生效")

        try:
            answer = await self._ai.complete(
                build_messages(config["prompt"], candidate_payload(message)),
                validate=lambda text: parse_candidate_decision(text),
                stream=False,
                scope=CHAIN_SCOPE_DEFAULT,
            )
            accepted, reason = parse_candidate_decision(answer.text)
        except AiError as exc:
            return self._failed(config["fallback"], exc, message)

        if accepted:
            return Admission(
                "accept", reason or "AI 判定通过", override=override
            )
        return Admission("reject", reason or "AI 判定拒绝", override=override)

    def _failed(
        self, fallback: str, exc: AiError, message: ParsedSourceMessage
    ) -> Admission:
        """A model chain that could not answer, resolved by the operator's switch.

        `reject` is the default because admitting a message on a failed decision
        is the surprising direction: the operator turned the gate on to keep
        things *out*, and a gate that vanishes on error is not a gate.
        """
        logger.warning(
            "ai_candidate_admission_failed",
            extra={
                "error_code": exc.code,
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "fallback": fallback,
            },
        )
        detail = f"AI 判定不可用：{exc.public_message}"
        if fallback == "accept":
            return Admission("accept", detail + "（按配置放行）")
        return Admission("reject", detail + "（按配置拒绝）")


__all__ = [
    "Admission",
    "CandidateAdmissionService",
    "parse_candidate_decision",
]
