"""AI candidate admission: the answer parser and the gate's behaviour.

The model is a fake, because what is under test is not HTTP but the decisions
around it: is the gate off when the operator never turned it on, does a chain
with no models silently do nothing rather than fail closed, and does a model
that answers prose walk the fallback rather than inventing a verdict.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.errors import AI_CANDIDATE_INVALID, AI_CHAIN_UNAVAILABLE, AiError
from app.ai.models import CHAIN_SCOPE_CANDIDATE, CHAIN_SCOPE_DEFAULT
from app.ai.prompt import DEFAULT_CANDIDATE_PROMPT, candidate_payload
from app.candidates.admission import (
    Admission,
    CandidateAdmissionService,
    parse_candidate_decision,
)
from app.candidates.models import ParsedSourceMessage


def _message(**overrides: object) -> ParsedSourceMessage:
    base: dict[str, object] = dict(
        is_edit=False,
        chat_id=-100,
        chat_title="Channel",
        message_id=7,
        sender_id=None,
        reply_to_message_id=None,
        media_group_id=None,
        message_text="书名\nhttps://exhentai.org/g/1/abc/",
        attachments=(),
        file_unique_id=None,
        message_date="2026-01-01T00:00:00+00:00",
        title="书名",
        title_source="TELEGRAM",
        title_confidence=0.9,
        filter_result="ACCEPT",
        filter_reason="",
        ex_gid=1,
        preview_urls=(),
    )
    base.update(overrides)
    return ParsedSourceMessage(**base)  # type: ignore[arg-type]


class FakeAi:
    def __init__(
        self,
        *,
        text: str = '{"accept": true, "reason": "是一本作品"}',
        chain: tuple = (SimpleNamespace(),),
        error: AiError | None = None,
    ) -> None:
        self.text = text
        self.chain = chain
        self.error = error
        self.messages: list[list[dict[str, str]]] = []
        self.scopes: list[str] = []

    async def effective_chain(self, scope: str = CHAIN_SCOPE_DEFAULT) -> tuple:
        return self.chain

    async def complete(self, messages, *, validate=None, stream=None, scope=CHAIN_SCOPE_DEFAULT):
        self.messages.append(list(messages))
        self.scopes.append(scope)
        if validate is not None:
            validate(self.text)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


class FakeSettings:
    def __init__(
        self, *, ai_enabled: bool = True, **overrides: object
    ) -> None:
        self._ai_enabled = ai_enabled
        self._config = {
            "enabled": True,
            "prompt": DEFAULT_CANDIDATE_PROMPT,
            "override_parse_rules": False,
            "fallback": "reject",
        }
        self._config.update(overrides)

    async def candidate_admission(self) -> dict[str, object]:
        return dict(self._config)

    async def ai_enabled(self) -> bool:
        return self._ai_enabled


class TestAnswerParsing:
    def test_a_clean_object_is_read(self) -> None:
        assert parse_candidate_decision(
            '{"accept": true, "reason": "作品"}'
        ) == (True, "作品")

    def test_padding_around_the_object_is_tolerated(self) -> None:
        accepted, reason = parse_candidate_decision(
            '好的，判断如下：\n```json\n{"accept": false, "reason": "广告"}\n```'
        )
        assert accepted is False
        assert reason == "广告"

    def test_a_missing_accept_field_is_refused(self) -> None:
        with pytest.raises(AiError) as raised:
            parse_candidate_decision('{"reason": "作品"}')
        assert raised.value.code == AI_CANDIDATE_INVALID

    def test_a_string_accept_is_refused(self) -> None:
        """`"accept": "yes"` is not an answer to the boolean question asked."""
        with pytest.raises(AiError) as raised:
            parse_candidate_decision('{"accept": "yes"}')
        assert raised.value.code == AI_CANDIDATE_INVALID

    def test_prose_with_no_object_is_refused(self) -> None:
        with pytest.raises(AiError):
            parse_candidate_decision("这是作品")


class TestPayload:
    def test_the_message_text_and_attachments_travel(self) -> None:
        payload = candidate_payload(
            _message(
                attachments=(
                    {"type": "archive", "file_name": "book.zip", "mime_type": "application/zip", "size_bytes": 10},
                )
            )
        )
        assert payload["source"] == "Channel"
        assert payload["has_gallery_link"] is True
        assert payload["attachments"] == [
            {
                "type": "archive",
                "file_name": "book.zip",
                "mime_type": "application/zip",
                "size_bytes": 10,
            }
        ]

    def test_the_payload_carries_no_account_or_chat_id(self) -> None:
        """The model is shown the message, not the deployment."""
        payload = candidate_payload(_message())
        assert set(payload) == {
            "source",
            "text",
            "has_gallery_link",
            "preview_links",
            "attachments",
        }


class TestGate:
    def test_disabled_means_skip_not_reject(self) -> None:
        service = CandidateAdmissionService(FakeAi(), FakeSettings(enabled=False))
        admission = _decide(service)
        assert admission.verdict == "skip"
        assert admission.override is False

    def test_an_enabled_gate_with_no_chain_does_not_take_effect(self) -> None:
        """「配置了提供商 + 手动开启」 is the condition; a half-configured gate
        must not stop ingestion."""
        ai = FakeAi(chain=())
        service = CandidateAdmissionService(ai, FakeSettings())
        admission = _decide(service)
        assert admission.verdict == "skip"
        assert ai.messages == []

    def test_accept_carries_the_override_switch(self) -> None:
        ai = FakeAi(text='{"accept": true, "reason": "作品"}')
        service = CandidateAdmissionService(
            ai, FakeSettings(override_parse_rules=True)
        )
        admission = _decide(service)
        assert admission.verdict == "accept"
        assert admission.reason == "作品"
        assert admission.override is True

    def test_reject_keeps_the_reason(self) -> None:
        ai = FakeAi(text='{"accept": false, "reason": "是广告"}')
        admission = _decide(CandidateAdmissionService(ai, FakeSettings()))
        assert admission.verdict == "reject"
        assert "广告" in admission.reason

    def test_the_candidate_scope_is_asked(self) -> None:
        """R52: the gate has its own chain scope, not the global default."""
        ai = FakeAi()
        _decide(CandidateAdmissionService(ai, FakeSettings()))
        assert ai.scopes == [CHAIN_SCOPE_CANDIDATE]

    def test_the_master_switch_skips_before_any_call(self) -> None:
        """生效 = 总开关 AND 本功能开关; off means zero model calls."""
        ai = FakeAi()
        admission = _decide(
            CandidateAdmissionService(ai, FakeSettings(ai_enabled=False))
        )
        assert admission.verdict == "skip"
        assert "全局关闭" in admission.reason
        assert ai.messages == []
        assert ai.scopes == []

    def test_a_malformed_answer_walks_to_the_fallback(self) -> None:
        ai = FakeAi(text="这不是 JSON")
        admission = _decide(CandidateAdmissionService(ai, FakeSettings()))
        assert admission.verdict == "reject"

    def test_the_fallback_can_be_accept(self) -> None:
        ai = FakeAi(text="这不是 JSON")
        admission = _decide(
            CandidateAdmissionService(ai, FakeSettings(fallback="accept"))
        )
        assert admission.verdict == "accept"

    def test_a_chain_failure_uses_the_fallback(self) -> None:
        ai = FakeAi(error=AiError(AI_CHAIN_UNAVAILABLE, "所有 AI 模型都不可用"))
        admission = _decide(CandidateAdmissionService(ai, FakeSettings()))
        assert admission.verdict == "reject"
        assert "不可用" in admission.reason

    def test_the_configured_prompt_is_sent(self) -> None:
        ai = FakeAi()
        _decide(CandidateAdmissionService(ai, FakeSettings(prompt="自定义提示")))
        assert ai.messages[0][0]["content"] == "自定义提示"


def _decide(service: CandidateAdmissionService) -> Admission:
    import asyncio

    return asyncio.run(service.decide(_message()))
