from __future__ import annotations

import asyncio

import httpx

from app.core.logging import get_logger
from app.schemas.stats import ProviderCredits

log = get_logger(__name__)

# ScrapingBee's account usage endpoint. It is FREE (does not consume credits)
# and returns the account's max/used API credit. See:
# https://www.scrapingbee.com/documentation/#usage-endpoint
_USAGE_URL = "https://app.scrapingbee.com/api/v1/usage"


async def _fetch_one(client: httpx.AsyncClient, api_key: str) -> tuple[int, int] | None:
    """Return (max_credit, used_credit) for one key, or None on failure."""
    try:
        resp = await client.get(_USAGE_URL, params={"api_key": api_key})
        resp.raise_for_status()
        data = resp.json()
        max_credit = int(data.get("max_api_credit", 0))
        used_credit = int(data.get("used_api_credit", 0))
        return max_credit, used_credit
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("scrapingbee_usage_lookup_failed", error=str(exc))
        return None


async def get_provider_credits(api_keys: list[str]) -> ProviderCredits:
    """Sum live ScrapingBee credits across every configured key.

    A pool of keys (the VPS setup) is treated as one combined balance. If no key
    is configured or every lookup fails, returns available=False so the UI can
    fall back to showing just the operational status.
    """
    if not api_keys:
        return ProviderCredits(available=False)

    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(*(_fetch_one(client, key) for key in api_keys))

    ok = [r for r in results if r is not None]
    if not ok:
        return ProviderCredits(available=False)

    max_total = sum(r[0] for r in ok)
    used_total = sum(r[1] for r in ok)
    return ProviderCredits(
        available=True,
        max_credits=max_total,
        used_credits=used_total,
        remaining_credits=max(max_total - used_total, 0),
    )
