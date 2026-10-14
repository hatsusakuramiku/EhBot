"""A failed preview route must finish all requests before returning."""
import asyncio

import httpx
import pytest

from app.telegraph.fetcher import FetchLimits, TelegraphFetcher
from app.telegraph.models import TelegraphError
from tests.unit.test_telegraph import public_resolver


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["failure", "timeout", "cancel"])
async def test_preview_requests_are_drained_before_route_returns(reason):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    cleanup_allowed = asyncio.Event()
    finished = asyncio.Event()

    async def transport(request):
        if request.url.path == "/fail":
            await entered.wait()
            return httpx.Response(200, content=b"not an image")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await cleanup_allowed.wait()
            finished.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        fetcher = TelegraphFetcher(
            client,
            FetchLimits(timeout_seconds=0.05 if reason == "timeout" else 5),
            resolver=public_resolver,
        )
        urls = ("https://pic.example/slow",)
        if reason == "failure":
            urls += ("https://pic.example/fail",)
        route = asyncio.create_task(fetcher.fetch_all("https://telegra.ph/page", urls))
        await asyncio.wait_for(entered.wait(), 2)
        if reason == "cancel":
            route.cancel()
        try:
            await asyncio.wait_for(cancelled.wait(), 0.5)
            assert not route.done(), "route returned while an image request was still active"
        finally:
            cleanup_allowed.set()
            if not cancelled.is_set():
                route.cancel()
            await asyncio.gather(route, return_exceptions=True)
        assert finished.is_set()
        if reason == "cancel":
            assert route.cancelled()
        else:
            with pytest.raises(TelegraphError):
                route.result()


@pytest.mark.asyncio
async def test_total_image_budget_stops_and_drains_remaining_request_before_timeout():
    entered, closing, released, closed = (asyncio.Event() for _ in range(4))
    image = b"\xff\xd8\xff" + b"x" * 100

    async def transport(request):
        if request.url.path != "/slow":
            await entered.wait()
            return httpx.Response(200, content=image, headers={"content-type": "image/jpeg"})
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await released.wait()
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        fetcher = TelegraphFetcher(client, FetchLimits(concurrency=3, timeout_seconds=5,
                                    max_total_bytes=len(image)), resolver=public_resolver)
        route = asyncio.create_task(fetcher.fetch_all("https://telegra.ph/page", tuple(
            f"https://pic.example/{name}" for name in ("one", "two", "slow"))))
        try:
            await asyncio.wait_for(closing.wait(), 0.5)
            assert not route.done(), "budget failure returned while a sibling request remained active"
        finally:
            released.set()
            if not closing.is_set():
                route.cancel()
            await asyncio.gather(route, return_exceptions=True)
        assert closed.is_set()
        with pytest.raises(TelegraphError) as failed:
            route.result()
        assert failed.value.code == "TELEGRAPH_LIMIT_EXCEEDED"
