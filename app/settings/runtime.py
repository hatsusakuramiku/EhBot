"""Values cached on `app.state` that a settings save has to refresh.

`route_source` and the page shell are synchronous, so they read the download
source priority and the display timezone from `app.state` rather than from the
database. Every write path that can change either one has to re-cache it.

That work lives here, once, rather than in the page layer's deps: the JSON layer
writes the same settings, and a save that skipped the re-cache would appear to do
nothing until the next restart -- the kind of divergence that only shows up in
production, hours later.

The service is passed in rather than resolved here, so each layer keeps its own
accessor and its own shape of "unavailable" (the page raises `HTTPException`, the
JSON layer raises `ApiError`).
"""

from __future__ import annotations

import asyncio
from fastapi import Request

from app.logging import apply_runtime_log_level


async def refresh_system(request: Request, service) -> None:
    """Refresh cached preferences in order, including after receipt replay."""
    state = request.app.state
    if not hasattr(state, 'settings_refresh_lock'):
        state.settings_refresh_lock = asyncio.Lock()
    async with state.settings_refresh_lock:
        snapshot = await service.snapshot()
        state.display_timezone = snapshot['timezone']
        state.download_source_priority = tuple(snapshot['download_source_priority'])
        apply_runtime_log_level(snapshot['log_level'])


async def refresh_display_timezone(request: Request, service) -> str:
    """Re-cache the zone the shell renders timestamps in."""
    zone = await service.timezone()
    request.app.state.display_timezone = zone
    return zone


async def refresh_download_source_priority(
    request: Request, service
) -> tuple[str, ...]:
    """Re-cache the order the router tries download sources in."""
    priority = await service.download_source_priority()
    request.app.state.download_source_priority = priority
    return priority
