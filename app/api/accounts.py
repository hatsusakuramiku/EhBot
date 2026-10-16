"""Shared administrator account configuration and command API."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.auth import _json_body
from app.api.contracts import ApiError
from app.connections.models import ProviderConnectionError


router = APIRouter(tags=["accounts"])


def _error(exc):
    code = exc.code
    status = 404 if code.endswith("NOT_FOUND") else 409 if code in {
        "REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT", "ACCOUNT_STOPPING", "ACCOUNT_ACTIVE",
        "ACCOUNT_IDENTITY_IN_USE", "ACCOUNT_IDENTITY_CHANGED", "ACCOUNT_RUNTIME_LIMIT", "ACCOUNT_LOGIN_STALE",
    } else 428 if code in {"PRECONDITION_REQUIRED", "IDEMPOTENCY_KEY_REQUIRED"} else 400
    return ApiError(code, exc.public_message, status_code=status)


async def _access(request, *, write=False):
    await deps.require_session(request)
    if write:
        deps.require_csrf(request)
    return deps.connection_manager(request)


@router.get("/accounts")
async def api_accounts(request: Request, response: Response):
    manager = await _access(request)
    response.headers["Cache-Control"] = "no-store"
    accounts = await manager.registry.list_accounts()
    return {"items": accounts, "total": len(accounts),
            "runtime": {account["account_id"]: manager.account_status(account["account_id"]) for account in accounts}}


@router.get("/accounts/operations/{operation_id}")
async def api_account_operation(request: Request, operation_id: str, response: Response):
    manager = await _access(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await manager.lifecycle.get_operation(operation_id)
    except ProviderConnectionError as exc:
        raise _error(exc) from None


@router.get("/accounts/commands/{key}")
async def api_account_command_receipt(request: Request, key: str, response: Response):
    manager = await _access(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await manager.command_receipt(key)
    except ProviderConnectionError as exc:
        raise _error(exc) from None


@router.get("/accounts/{account_id}")
async def api_account(request: Request, account_id: str, response: Response):
    manager = await _access(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await manager.registry.get(account_id)
    except ProviderConnectionError as exc:
        raise _error(exc) from None


@router.post("/accounts/{account_id}/reveal")
async def api_account_reveal(request: Request, account_id: str):
    # Explicit reveal uses the same CSRF rule as writes; cookies alone cannot
    # trigger sensitive configuration disclosure through another origin.
    manager = await _access(request, write=True)
    payload = await _json_body(request)
    try:
        manager.registry._expected(payload.get("expected_revision"))
        revealed = await manager.registry.reveal(account_id)
        if revealed["revision"] != payload["expected_revision"]:
            raise ApiError("REVISION_CONFLICT", "账号已改变，请刷新后核对", status_code=409)
        return JSONResponse(revealed, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except ProviderConnectionError as exc:
        raise _error(exc) from None


@router.post("/accounts/commands")
async def api_account_command(request: Request):
    manager = await _access(request, write=True)
    payload = deps.command_payload(request, await _json_body(request))
    action = payload.get("action")
    if not isinstance(action, str):
        raise ApiError("ACCOUNT_ACTION_INVALID", "请指定账号命令")
    allowed = {
        "create": {"kind", "label", "fields"}, "rename": {"label"},
        "edit": {"label", "fields", "clear_fields"}, "disable": set(), "delete": set(),
        "test": set(), "enable": set(), "login-start": set(),
        "login-complete": {"challenge_id", "code", "password"}, "login-cancel": {"challenge_id"},
    }
    if action not in allowed:
        raise ApiError("ACCOUNT_ACTION_INVALID", "不支持的账号命令")
    common = {"action", "account_id", "expected_revision", "idempotency_key"}
    if set(payload) - common - allowed[action]:
        raise ApiError("BODY_INVALID", "账号命令含未知字段")
    params = {key: payload[key] for key in allowed[action] if key in payload}
    if action.startswith("login-") and action != "login-start":
        if not isinstance(params.get("challenge_id"), str) or not params["challenge_id"]:
            raise ApiError("ACCOUNT_CHALLENGE_REQUIRED", "请提交登录挑战身份")
        for key in ("code", "password"):
            if key in params and not isinstance(params[key], str):
                raise ApiError("BODY_INVALID", "登录字段类型无效")
    try:
        result = await manager.command(action, account_id=payload.get("account_id"),
                                       expected_revision=payload.get("expected_revision"),
                                       idempotency_key=payload.get("idempotency_key"), **params)
    except ProviderConnectionError as exc:
        raise _error(exc) from None
    if result.get("status") == "FAILED":
        error = result["error"]
        return JSONResponse({"error": {**error, "details": {"command_id": result["command_id"]}}},
                            status_code=409 if error["code"] in {"REVISION_CONFLICT", "ACCOUNT_RUNTIME_LIMIT", "ACCOUNT_LOGIN_STALE"} else 400,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(result, status_code=202 if result.get("status") in {"PENDING", "UNKNOWN"} else 200,
                        headers={"Cache-Control": "no-store"})
