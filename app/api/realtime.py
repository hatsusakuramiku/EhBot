"""Authenticated realtime transport over the existing, validated HTTP routes.

Commands are named entries in a closed catalogue, never caller-supplied URLs.
The ASGI transport runs locally: authentication, validation and business rules
are identical to HTTP, without a second implementation of every operation.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import ExitStack
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRoute

from app.api import deps
from app.api.contracts import ApiError
from app.api.events import EVENT_REFRESH
from app.api.logs import buffered_entry
from app.public_url import normalize_origin


router = APIRouter()
PROTOCOL = "ehbot-realtime.v1"
HEARTBEAT_SECONDS = 15
MAX_FRAME_BYTES = 262144


def command_catalogue() -> dict[str, dict]:
    """Publish only finite JSON/HTML routes; exclude auth, streams and files."""
    from app.api import accounts, actions, activity, candidates, downloaded, logs, settings, summary, works
    from app.web.routes import (
        activity as web_activity, auto_approval, candidates as web_candidates,
        connections, dashboard, downloaded as web_downloaded, logs as web_logs,
        manual_add, settings_pages, works as web_works,
    )
    catalogue = {}
    groups = (
        ("api", "/api/v1", [accounts, actions, activity, candidates, downloaded, logs, settings, summary, works]),
        ("web", "", [web_activity, auto_approval, web_candidates, connections, dashboard,
                     web_downloaded, web_logs, manual_add, settings_pages, web_works]),
    )
    for namespace, prefix, modules in groups:
        for module in modules:
            for route in module.router.routes:
                if (not isinstance(route, APIRoute) or route.path.endswith("/stream")
                        or route.name == "api_account_reveal"):
                    continue
                method = next(iter(route.methods))
                name = f"{namespace}.{route.name}"
                assert name not in catalogue, f"duplicate realtime command: {name}"
                catalogue[name] = {
                    "method": method, "path": prefix + route.path,
                    "params": list(route.param_convertors),
                    "encoding": "json" if namespace == "api" else "form",
                }
    return catalogue


def _path(entry: dict, params: dict) -> str:
    if not isinstance(params, dict) or set(params) != set(entry["params"]):
        raise ApiError("COMMAND_PARAMS_INVALID", "命令路径参数不正确", status_code=400)
    path = entry["path"]
    for name, value in params.items():
        text = str(value)
        if not text or text in {".", ".."} or any(c in text for c in "/\\?#%"):
            raise ApiError("COMMAND_PARAMS_INVALID", "命令路径参数不正确", status_code=400)
        path = path.replace("{" + name + "}", quote(text, safe=""))
    return path


def _pairs(value) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        value = [[key, item] for key, item in value.items()]
    if not isinstance(value, list) or any(
        not isinstance(pair, (list, tuple)) or len(pair) != 2
        or not all(isinstance(item, str) for item in pair) for pair in value
    ):
        raise ApiError("COMMAND_BODY_INVALID", "表单或查询参数不正确", status_code=400)
    return [tuple(pair) for pair in value]


class OperationEventsMiddleware:
    """Notify other clients after an HTTP or WebSocket operation commits."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, capture)
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if 200 <= status < 400 and scope.get("app"):
            app = scope["app"]
            path = scope["path"]
            app.state.event_bus.publish(EVENT_REFRESH, path=path)


@router.get("/api/v1/realtime")
async def realtime_contract(request: Request):
    """Authenticated command directory for WebUI and native clients."""
    await deps.require_session(request)
    bus = request.app.state.event_bus
    return {
        "protocol": PROTOCOL, "path": request.scope.get("root_path", "") + "/ws/ui",
        "epoch": bus.epoch, "sequence": bus.sequence,
        "heartbeat_seconds": HEARTBEAT_SECONDS, "replay": False,
        "commands": command_catalogue(),
    }


@router.websocket("/ws/ui")
async def realtime_socket(ws: WebSocket):
    """One authenticated connection; bounded queues and serialized commands."""
    try:
        await deps.require_session(ws)
        bearer = getattr(ws.state, "auth_source", None) == "bearer"
        origin = ws.headers.get("origin")
        expected_origin = ("https://" if ws.url.scheme == "wss" else "http://") + ws.url.netloc
        try:
            matches_origin = bool(origin and normalize_origin(origin) == normalize_origin(expected_origin))
        except ValueError:
            matches_origin = False
        if (origin and not matches_origin) or (not bearer and not origin):
            raise ApiError("ORIGIN_INVALID", "连接来源不正确", status_code=403)
    except ApiError:
        await ws.close(code=4401)
        return

    await ws.accept()
    send_lock = asyncio.Lock()

    async def send(payload):
        async with send_lock:
            await asyncio.wait_for(ws.send_json(payload), timeout=10)

    async def read():
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        raw = message.get("text")
        if not isinstance(raw, str):
            raise ApiError("FRAME_INVALID", "消息必须是 JSON 文本", status_code=400)
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ApiError("FRAME_TOO_LARGE", "消息过大", status_code=400)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ApiError("FRAME_INVALID", "消息不是有效 JSON", status_code=400)
        if not isinstance(payload, dict):
            raise ApiError("FRAME_INVALID", "消息必须是 JSON 对象", status_code=400)
        return payload

    try:
        hello = await asyncio.wait_for(read(), timeout=10)
        if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL:
            raise ApiError("PROTOCOL_UNSUPPORTED", "实时协议版本不匹配", status_code=400)
        if hello.get("client") not in {"web", "mobile"}:
            raise ApiError("CLIENT_INVALID", "客户端类型不正确", status_code=400)
        if not bearer:
            supplied = hello.get("csrf_token")
            expected = ws.session.get("csrf_token", "")
            if not isinstance(supplied, str) or not expected or not hmac.compare_digest(supplied, expected):
                raise ApiError("CSRF_INVALID", "请求校验失败，请重新登录", status_code=403)
        if bearer and hello["client"] == "web":
            raise ApiError("CLIENT_INVALID", "WebUI 使用浏览器会话", status_code=400)
    except (ApiError, TimeoutError) as exc:
        error = exc.to_payload()["error"] if isinstance(exc, ApiError) else {"code": "HELLO_TIMEOUT", "message": "握手超时"}
        await send({"type": "error", "error": error, "protocol": PROTOCOL})
        await ws.close(code=4403)
        return
    except WebSocketDisconnect:
        return

    catalogue = command_catalogue()
    if hello["client"] == "mobile":
        catalogue = {name: item for name, item in catalogue.items() if name.startswith("api.")}
    bus = ws.app.state.event_bus
    # A browser cookie has the same absolute lifetime on an active socket as it
    # has on a new HTTP request; heartbeats never renew it invisibly.
    cookie_deadline = None
    if not bearer:
        _, issued = ws.app.state.realtime_cookie_signer.unsign(ws.cookies["session"], return_timestamp=True)
        cookie_deadline = issued.timestamp() + 14 * 24 * 3600

    async def authorize():
        await deps.require_session(ws)
        if not bearer and time.time() >= cookie_deadline:
            raise ApiError("NOT_AUTHENTICATED", "会话已结束，请重新登录", status_code=401)

    headers = {name: ws.headers[name] for name in ("authorization",) if name in ws.headers}
    if not bearer:
        headers[deps.CSRF_HEADER] = ws.session["csrf_token"]
    headers["HX-Request"] = "true"
    root = ws.scope.get("root_path", "")
    http_origin = "https://" + ws.url.netloc if ws.url.scheme == "wss" else "http://" + ws.url.netloc
    transport = httpx.ASGITransport(app=ws.app, root_path=root, raise_app_exceptions=False, client=(ws.client.host, ws.client.port))
    commands: asyncio.Queue = asyncio.Queue(maxsize=8)
    seen: set[str] = set()
    order: deque[str] = deque()
    last_received = time.monotonic()
    tasks = []

    async with httpx.AsyncClient(transport=transport, base_url=http_origin, headers=headers, cookies=dict(ws.cookies)) as client:
        async def execute(frame):
            request_id = frame["request_id"]
            try:
                await authorize()
                name = frame.get("name")
                entry = catalogue.get(name) if isinstance(name, str) else None
                if entry is None:
                    raise ApiError("COMMAND_UNKNOWN", "未知的实时命令", status_code=400)
                payload = frame.get("payload", {})
                if not isinstance(payload, dict):
                    raise ApiError("COMMAND_BODY_INVALID", "命令参数必须是对象", status_code=400)
                path = root + _path(entry, payload.get("params", {}))
                query = _pairs(payload.get("query", []))
                options = {"params": query}
                if entry["encoding"] == "json":
                    if "body" in payload:
                        options["json"] = payload["body"]
                else:
                    body = _pairs(payload.get("body", []))
                    body = [(k, v) for k, v in body if k != "csrf_token"]
                    if not bearer:
                        body.append(("csrf_token", ws.session["csrf_token"]))
                    options.update(content=urlencode(body), headers={"Content-Type": "application/x-www-form-urlencoded"})
                    if entry["method"] != "GET":
                        context = payload.get("context", "")
                        if isinstance(context, str):
                            context_url = urlsplit(context)
                            if not context_url.netloc and context_url.path.startswith("/"):
                                supplied_keys = {key for key, _ in query}
                                options["params"] = [(k, v) for k, v in parse_qsl(context_url.query, keep_blank_values=True)
                                                     if k not in supplied_keys] + query
                # The start watermark is conservative. If an event occurs
                # during this read, the client must reconcile again afterwards.
                watermark = bus.sequence
                response = await client.request(entry["method"], path, **options)
                if entry["encoding"] == "form":
                    for _ in range(4):
                        if not response.is_redirect:
                            break
                        target = urlsplit(response.headers["location"])
                        if target.netloc and target.netloc != ws.url.netloc:
                            raise ApiError("REDIRECT_INVALID", "操作返回了外部地址", status_code=500)
                        # Keep the visible filter/page for a redirect back to
                        # this list; notices from the action take precedence.
                        context = payload.get("context", "")
                        local = urlsplit(context) if isinstance(context, str) else urlsplit("")
                        redirected = target.path
                        if local.path == target.path:
                            redirected_pairs = parse_qsl(target.query, keep_blank_values=True)
                            replaced_keys = {key for key, _ in redirected_pairs}
                            merged = [(k, v) for k, v in parse_qsl(local.query, keep_blank_values=True)
                                      if k not in replaced_keys] + redirected_pairs
                            redirected += "?" + urlencode(merged)
                        elif target.query:
                            redirected += "?" + target.query
                        if not any(
                            item["method"] == "GET" and _matches(item["path"], target.path.removeprefix(root))
                            for item in catalogue.values()
                        ):
                            raise ApiError("NOT_AUTHENTICATED", "页面需要重新登录", status_code=401)
                        watermark = bus.sequence
                        response = await client.get(redirected)
                fmt = "json" if response.headers.get("content-type", "").startswith("application/json") else "html"
                data = response.json() if fmt == "json" else response.text
                result = {"request_id": request_id, "status": response.status_code, "format": fmt,
                          "data": data, "epoch": bus.epoch, "sequence": watermark,
                          "location": response.url.raw_path.decode("ascii")}
                redirect_error = dict(parse_qsl(response.url.query.decode("ascii"))).get("error") if fmt == "html" else None
                if response.status_code >= 400 or redirect_error:
                    error = data.get("error") if isinstance(data, dict) else None
                    if not error and isinstance(data, dict) and "detail" in data:
                        detail = data["detail"]
                        message = "; ".join(".".join(map(str, item["loc"])) + ": " + item["msg"] for item in detail) if isinstance(detail, list) else str(detail)
                        error = {"code": "REQUEST_REJECTED", "message": message}
                    result.update(type="command.error", error=error or {"code": "OPERATION_FAILED", "message": redirect_error or "操作失败，请查看页面提示"})
                else:
                    result["type"] = "command.result"
                await send(result)
            except ApiError as exc:
                await send({"type": "command.error", "request_id": request_id, "error": exc.to_payload()["error"]})
            except Exception:
                logging.getLogger(__name__).exception("realtime_command_failed")
                await send({"type": "command.error", "request_id": request_id,
                            "error": {"code": "INTERNAL_ERROR", "message": "操作执行失败，请先核对当前状态"}})

        async def worker():
            while True:
                await execute(await commands.get())

        async def receive():
            nonlocal last_received
            while True:
                frame = await read()
                await authorize()
                last_received = time.monotonic()
                if frame.get("type") == "pong":
                    continue
                request_id = frame.get("request_id")
                if frame.get("type") != "command" or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                    await send({"type": "error", "error": {"code": "COMMAND_INVALID", "message": "命令或请求标识不正确"}})
                    continue
                if request_id in seen or commands.full():
                    code = "COMMAND_DUPLICATE" if request_id in seen else "COMMAND_BUSY"
                    await send({"type": "command.error", "request_id": request_id, "error": {"code": code, "message": "重复请求或操作过于频繁"}})
                    continue
                if len(order) >= 4096:
                    seen.discard(order.popleft())
                seen.add(request_id)
                order.append(request_id)
                await send({"type": "command.accepted", "request_id": request_id})
                commands.put_nowait(frame)

        async def events(queue):
            while True:
                event = await queue.get()
                await send({"type": "state", "epoch": bus.epoch, "sequence": event.sequence,
                            "scope": event.name, "data": event.data, "refresh_required": True})

        async def logs(queue):
            replay = ws.app.state.log_broker.snapshot(limit=50)
            highest = replay[0].sequence if replay else 0
            for record in reversed(replay):
                await send({"type": "log", "sequence": record.sequence, "epoch": bus.epoch, "data": buffered_entry(record.line)})
            while True:
                record = await queue.get()
                if record.sequence <= highest:
                    continue
                await send({"type": "log", "sequence": record.sequence, "epoch": bus.epoch, "data": buffered_entry(record.line)})

        async def heartbeat():
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await authorize()
                if time.monotonic() - last_received > HEARTBEAT_SECONDS * 3:
                    await ws.close(code=4408)
                    return
                await send({"type": "ping", "epoch": bus.epoch, "sequence": bus.sequence})

        with ExitStack() as stack:
            queue = stack.enter_context(bus.subscribe())
            await send({"type": "ready", "protocol": PROTOCOL, "epoch": bus.epoch,
                        "sequence": bus.sequence, "heartbeat_seconds": HEARTBEAT_SECONDS,
                        "replay": False, "commands": catalogue})
            tasks = [asyncio.create_task(fn) for fn in (worker(), receive(), events(queue), heartbeat())]
            if hello.get("logs") is True:
                log_queue = stack.enter_context(ws.app.state.log_broker.subscribe())
                tasks.append(asyncio.create_task(logs(log_queue)))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except ApiError as exc:
                await send({"type": "error", "error": exc.to_payload()["error"]})
                await ws.close(code=4401)
            except (WebSocketDisconnect, RuntimeError, TimeoutError):
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def _matches(template: str, path: str) -> bool:
    """Match one registered route, including only single-segment parameters."""
    left, right = template.split("/"), path.split("/")
    return len(left) == len(right) and all(
        a == b or (a.startswith("{") and a.endswith("}") and bool(b))
        for a, b in zip(left, right)
    )
