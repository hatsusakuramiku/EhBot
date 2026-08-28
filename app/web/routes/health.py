"""The container's two probes.

`/healthz` answers as soon as the process is up; `/readyz` re-checks the three
writable directories and the database on every call, because a volume can be
remounted read-only long after a successful start.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.storage.readiness import ensure_writable_directory
from app.web import deps

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    errors = list(request.app.state.startup_errors)
    if not await deps.database(request).check_writable():
        errors.append("database is not writable")
    for name, path in (
        ("data", deps.settings(request).data_path),
        ("library", deps.settings(request).library_path),
        ("work", deps.settings(request).work_path),
    ):
        try:
            await asyncio.to_thread(ensure_writable_directory, path)
        except OSError:
            errors.append(f"{name} directory is not writable")
    if errors:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "errors": errors},
        )
    return JSONResponse(content={"status": "ready"})
