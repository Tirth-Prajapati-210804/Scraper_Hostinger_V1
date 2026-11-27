from __future__ import annotations

import httpx
import pytest

from app.services import provider_credits_service
from app.services.provider_credits_service import (
    get_active_provider_credits,
    get_provider_credits,
    get_scrapedo_credits,
)


@pytest.mark.asyncio
async def test_get_scrapedo_credits_parses_info_and_uses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_credits_service._scrapedo_cache.clear()
    calls = 0

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, params: dict | None = None):
            nonlocal calls
            calls += 1
            assert url == "https://api.scrape.do/info"
            assert params == {"token": "sd-key-1"}
            return httpx.Response(
                200,
                json={
                    "IsActive": True,
                    "MaxMonthlyRequest": 10000,
                    "RemainingMonthlyRequest": 9310,
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(provider_credits_service.httpx, "AsyncClient", MockAsyncClient)

    first = await get_scrapedo_credits(["sd-key-1"])
    second = await get_scrapedo_credits(["sd-key-1"])

    assert calls == 1
    assert first.available is True
    assert first.provider == "scrapedo"
    assert first.max_credits == 10000
    assert first.used_credits == 690
    assert first.remaining_credits == 9310
    assert second == first


@pytest.mark.asyncio
async def test_get_active_provider_credits_prefers_scrapedo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_credits_service._scrapedo_cache.clear()

    class MockSettings:
        scrapedo_base_url = "https://api.scrape.do"

        def get_scrapedo_keys(self) -> list[str]:
            return ["sd-key"]

        def get_scrapingbee_keys(self) -> list[str]:
            return ["sb-key"]

    called_urls: list[str] = []

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, params: dict | None = None):
            called_urls.append(url)
            return httpx.Response(
                200,
                json={"MaxMonthlyRequest": 1250000, "RemainingMonthlyRequest": 1000000},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(provider_credits_service.httpx, "AsyncClient", MockAsyncClient)

    result = await get_active_provider_credits(MockSettings())
    assert called_urls == ["https://api.scrape.do/info"]
    assert result.provider == "scrapedo"
    assert result.remaining_credits == 1000000
    assert result.used_credits == 250000


@pytest.mark.asyncio
async def test_get_provider_credits_returns_unavailable_when_empty() -> None:
    res = await get_provider_credits([])
    assert res.available is False
