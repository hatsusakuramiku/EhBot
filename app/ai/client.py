"""One OpenAI-compatible chat completion, and the errors it can raise.

Transport only: it is handed a base URL, a key, a model and the messages, and it
returns the assistant's text. Which key to use, which model to try next and what
to do with a failure is `app/ai/service.py` -- that separation is what lets the
rotation and fallback rules be tested without a network stack, and this class be
tested with one.

Compatible rather than OpenAI-specific on purpose: the same shape covers
OpenAI, DeepSeek, OpenRouter and the local servers (Ollama, LM Studio) an
operator may prefer precisely because nothing leaves the host. `response_format`
is deliberately not sent -- several of those servers reject unknown fields, and
the prompt already asks for JSON; the parser is lenient instead.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

import httpx

from app.ai.errors import AiError


#: The endpoint answered with a key it will not accept.
AI_AUTH = "AI_AUTH"
#: 429: this key is over its quota or rate. Rotate, cool down.
AI_RATE_LIMIT = "AI_RATE_LIMIT"
#: 404 / "model not found": this model name is wrong or gone. Next model.
AI_MODEL_MISSING = "AI_MODEL_MISSING"
#: The request did not finish inside the provider's timeout.
AI_TIMEOUT = "AI_TIMEOUT"
#: DNS/TCP/TLS: the address is unreachable.
AI_UNREACHABLE = "AI_UNREACHABLE"
#: 5xx: the provider is having a moment, not the configuration.
AI_SERVER_ERROR = "AI_SERVER_ERROR"
#: Any other 4xx: the body or the parameters were refused.
AI_REQUEST_REJECTED = "AI_REQUEST_REJECTED"
#: A 200 whose body is not a chat completion we can read.
AI_BAD_RESPONSE = "AI_BAD_RESPONSE"


class AiClientError(AiError):
    """A failed request, classified by what the caller should do about it.

    `key_fault` means 「换个 Key」 (the credential is the problem), `retryable`
    means 「同一把 Key 再试一次说不定就好了」 (transient). The two are separate because
    a timeout is worth one retry but not a new key, and a 401 is worth a new
    key but never a retry.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        key_fault: bool = False,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(code, message)
        self.key_fault = key_fault
        self.retryable = retryable
        self.status = status


def _provider_message(response: httpx.Response) -> str:
    """The provider's own sentence about a refusal, if it sent one.

    Never includes request headers, so the key cannot ride out this way; the
    text is truncated because some providers answer a 400 with a page of HTML.
    """
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])[:300]
        if isinstance(error, str):
            return error[:300]
    return ""


def _content_of(payload: Any) -> str:
    """The assistant text out of a chat completion, or a refusal.

    `content` is a string for every OpenAI-shaped server we care about, but the
    multimodal shape (a list of parts) exists and answering 「bad response」 for
    a body that plainly carries text would be wrong.
    """
    if not isinstance(payload, dict):
        raise AiClientError(AI_BAD_RESPONSE, "AI 返回的不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiClientError(AI_BAD_RESPONSE, "AI 返回里没有 choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    raise AiClientError(AI_BAD_RESPONSE, "AI 返回里没有可读的文本内容")


def _delta_of(payload: Any) -> str:
    """The incremental text out of one SSE chunk of a chat completion.

    The streaming shape mirrors the buffered one one level down: `choices[0]
    .message.content` becomes `choices[0].delta.content`. A chunk with no text --
    a role-only opening, a keep-alive, a final `finish_reason` marker -- yields
    the empty string rather than a refusal, because 「这一块没有文字」 is normal in
    a stream and only a stream with *no* text at all is a bad response.
    """
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    delta = first.get("delta") if isinstance(first, dict) else None
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


async def _collect_stream(response: httpx.Response) -> str:
    """Read an SSE chat-completion stream to its end and return the whole text.

    Line-oriented rather than byte-oriented: the OpenAI streaming format is one
    `data: {json}` per event, ended by `data: [DONE]`, and every compatible
    server (including the local ones) follows it. Anything that is not a `data:`
    line -- the `event:`/`id:` fields some servers add, comments, blank
    separators -- is skipped, so a server being chatty is not a failure.
    """
    chunks: list[str] = []
    async for line in response.aiter_lines():
        text = line.strip()
        if not text.startswith("data:"):
            continue
        data = text[5:].strip()
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
        except (ValueError, json.JSONDecodeError):
            continue
        chunks.append(_delta_of(payload))
    joined = "".join(chunks)
    if not joined.strip():
        raise AiClientError(
            AI_BAD_RESPONSE, "AI 的流式返回里没有可读的文本内容"
        )
    return joined


def _classify_status(status: int, detail: str) -> AiClientError:
    """Map an HTTP failure onto the code that decides what happens next."""
    suffix = f"：{detail}" if detail else ""
    if status in (401, 403):
        return AiClientError(
            AI_AUTH,
            f"AI 供应商拒绝了这把 API Key（HTTP {status}）{suffix}",
            key_fault=True,
            status=status,
        )
    if status == 429:
        return AiClientError(
            AI_RATE_LIMIT,
            f"AI 供应商限流或额度用尽（HTTP 429）{suffix}",
            key_fault=True,
            status=status,
        )
    if status == 404:
        return AiClientError(
            AI_MODEL_MISSING,
            f"AI 供应商没有这个模型（HTTP 404）{suffix}",
            status=status,
        )
    if status >= 500:
        return AiClientError(
            AI_SERVER_ERROR,
            f"AI 供应商服务出错（HTTP {status}）{suffix}",
            retryable=True,
            status=status,
        )
    return AiClientError(
        AI_REQUEST_REJECTED,
        f"AI 供应商拒绝了这次请求（HTTP {status}）{suffix}",
        status=status,
    )


class OpenAiCompatibleClient:
    """POST one `/chat/completions` and return the assistant's text."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self._http = http_client
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = float(timeout_seconds)
        self._max_retries = max(0, int(max_retries))

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        stream: bool = False,
    ) -> str:
        """Ask the model, retrying only what a retry can fix.

        The retry loop is here and not in the caller because it is about this
        *request*: a timeout or a 5xx may pass on the second try, the same way,
        with the same key. A 401 will not, and a 429 needs a different key --
        both are handed back for the service to rotate.

        `stream` changes the transport and nothing else: the same prompt, the
        same text back, gathered from the SSE parts instead of read out of one
        JSON body. A provider that ignores the flag and answers with a plain
        body is read the plain way, so turning it on cannot break a server that
        does not implement it.

        The response is streamed for its *transfer*; the whole text is still
        returned. This module owns 「how to talk to the provider」, and its caller
        wants an answer, not a channel.
        """
        body = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if stream:
            body["stream"] = True
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last: AiClientError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                status, detail, text = await self._post(
                    url, body, headers, stream=stream
                )
            except httpx.TimeoutException:
                last = AiClientError(
                    AI_TIMEOUT,
                    f"请求 AI 供应商超过 {self._timeout:.0f} 秒未完成",
                    retryable=True,
                )
            except httpx.HTTPError as exc:
                last = AiClientError(
                    AI_UNREACHABLE,
                    f"无法连接 AI 供应商：{type(exc).__name__}",
                    retryable=True,
                )
            else:
                if status >= 400:
                    last = _classify_status(status, detail)
                else:
                    return text
            if not last.retryable or attempt >= self._max_retries:
                break
        assert last is not None
        raise last

    async def _post(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        stream: bool,
    ) -> tuple[int, str, str]:
        """One HTTP round trip as `(status, provider_detail, assistant_text)`.

        Two code paths because `httpx` needs two: a buffered `post` for the
        ordinary case, and `stream` -- which must be opened, consumed and closed
        inside one `async with` -- for SSE. Both are normalised here so the
        retry loop has one place to classify a failure.

        A streaming request that comes back with a non-SSE content type is read
        as an ordinary JSON body on purpose: some gateways answer that way, and
        「it ignored my flag」 must not read as 「the provider is broken」.

        A body that is neither readable JSON nor readable SSE raises
        `AI_BAD_RESPONSE` here rather than being retried: the same bytes would
        come back, and the caller's next move is another model, not another try.
        """
        if not stream:
            response = await self._http.post(
                url, json=body, headers=headers, timeout=self._timeout
            )
            if response.status_code >= 400:
                return response.status_code, _provider_message(response), ""
            return response.status_code, "", _read_json_text(response)
        async with self._http.stream(
            "POST", url, json=body, headers=headers, timeout=self._timeout
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                return response.status_code, _provider_message(response), ""
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                return response.status_code, "", await _collect_stream(response)
            await response.aread()
            return response.status_code, "", _read_json_text(response)


def _read_json_text(response: httpx.Response) -> str:
    """The assistant text out of a buffered response, or a refusal."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise AiClientError(AI_BAD_RESPONSE, "AI 返回的不是 JSON") from exc
    return _content_of(payload)


__all__ = [
    "AI_AUTH",
    "AI_BAD_RESPONSE",
    "AI_MODEL_MISSING",
    "AI_RATE_LIMIT",
    "AI_REQUEST_REJECTED",
    "AI_SERVER_ERROR",
    "AI_TIMEOUT",
    "AI_UNREACHABLE",
    "AiClientError",
    "OpenAiCompatibleClient",
]
