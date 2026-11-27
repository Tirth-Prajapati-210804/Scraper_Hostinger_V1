from __future__ import annotations

import asyncio
from time import monotonic

import httpx

from app.core.logging import get_logger
from app.schemas.stats import ProviderCredits

log = get_logger(__name__)

# ScrapingBee's account usage endpoint. It is FREE (does not consume credits).
# https://www.scrapingbee.com/documentation/#usage-endpoint
_SCRAPINGBEE_USAGE_URL = "https://app.scrapingbee.com/api/v1/usage"
_SCRAPEDO_DEFAULT_BASE_URL = "https://api.scrape.do"

# Scrape.do caps `/info` at 10 req/min; cache briefly so polling UI tabs
# never trip 429 rate limits.
_SCRAPEDO_CACHE_TTL_SECONDS = 20.0
_scrapedo_cache: dict[str, tuple[float, ProviderCredits]] = {}


async def _fetch_scrapingbee_one(client: httpx.AsyncClient, api_key: str) -> tuple[int, int] | None:
    """Return (max_credit, used_credit) for one ScrapingBee key, or None."""
    try:
        resp = await client.get(_SCRAPINGBEE_USAGE_URL, params={"api_key": api_key})
        resp.raise_for_status()
        data = resp.json()
        max_credit = int(data.get("max_api_credit", 0))
        used_credit = int(data.get("used_api_credit", 0))
        return max_credit, used_credit
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("scrapingbee_usage_lookup_failed", error=str(exc))
        return None


async def _fetch_scrapedo_one(
    client: httpx.AsyncClient,
    api_key: str,
    info_url: str,
) -> tuple[int, int, int] | None:
    """Return (max_credit, used_credit, remaining_credit) for one Scrape.do token, or None."""
    try:
        resp = await client.get(info_url, params={"token": api_key})
        resp.raise_for_status()
        data = resp.json()
        max_credit = int(data.get("MaxMonthlyRequest", 0))
        remaining_credit = max(0, int(data.get("RemainingMonthlyRequest", 0)))
        used_credit = max(max_credit - remaining_credit, 0)
        return max_credit, used_credit, remaining_credit
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("scrapedo_usage_lookup_failed", error=str(exc))
        return None


async def get_provider_credits(api_keys: list[str]) -> ProviderCredits:
    """Sum live ScrapingBee credits across every configured key."""
    if not api_keys:
        return ProviderCredits(available=False)

    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(*(_fetch_scrapingbee_one(client, key) for key in api_keys))

    ok = [r for r in results if r is not None]
    if not ok:
        return ProviderCredits(available=False)

    max_total = sum(r[0] for r in ok)
    used_total = sum(r[1] for r in ok)
    return ProviderCredits(
        available=True,
        provider="scrapingbee",
        max_credits=max_total,
        used_credits=used_total,
        remaining_credits=max(max_total - used_total, 0),
    )


async def get_scrapedo_credits(
    api_keys: list[str],
    *,
    base_url: str = _SCRAPEDO_DEFAULT_BASE_URL,
) -> ProviderCredits:
    """Sum live Scrape.do monthly credits across configured keys via `/info`."""
    if not api_keys:
        return ProviderCredits(available=False)

    cleaned_base = (base_url or _SCRAPEDO_DEFAULT_BASE_URL).rstrip("/")
    info_url = f"{cleaned_base}/info"
    cache_key = f"{info_url}:{'|'.join(api_keys)}"
    now = monotonic()
    cached = _scrapedo_cache.get(cache_key)
    if cached is not None and (now - cached[0]) < _SCRAPEDO_CACHE_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=8.0) as client:
        tasks = [_fetch_scrapedo_one(client, key, info_url) for key in api_keys]
        results = await asyncio.gather(*tasks)

    ok = [r for r in results if r is not None]
    if not ok:
        return ProviderCredits(available=False, provider="scrapedo")

    max_total = sum(r[0] for r in ok)
    used_total = sum(r[1] for r in ok)
    remaining_total = sum(r[2] for r in ok)
    credits = ProviderCredits(
        available=True,
        provider="scrapedo",
        max_credits=max_total,
        used_credits=used_total,
        remaining_credits=remaining_total,
    )
    _scrapedo_cache[cache_key] = (now, credits)
    return credits


async def get_active_provider_credits(settings) -> ProviderCredits:
    """Fetch credits for the primary configured provider (Scrape.do first, else ScrapingBee)."""
    scrapedo_keys = (
        settings.get_scrapedo_keys()
        if callable(getattr(settings, "get_scrapedo_keys", None))
        else []
    )
    if scrapedo_keys:
        return await get_scrapedo_credits(
            scrapedo_keys,
            base_url=getattr(settings, "scrapedo_base_url", _SCRAPEDO_DEFAULT_BASE_URL),
        )
    scrapingbee_keys = settings.get_scrapingbee_keys()
    return await get_provider_credits(scrapingbee_keys)
