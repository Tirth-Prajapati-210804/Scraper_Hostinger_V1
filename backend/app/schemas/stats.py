from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ProviderStat(BaseModel):
    configured: bool
    last_success: datetime | None = None
    success_rate: float | None = None


class OverviewStats(BaseModel):
    active_route_groups: int
    total_prices_collected: int
    total_origins: int
    total_destinations: int
    last_collection_at: datetime | None
    last_collection_status: str | None
    provider_stats: dict[str, ProviderStat]


class ProviderCredits(BaseModel):
    """Live ScrapingBee credit balance, summed across all configured keys."""

    available: bool  # False when no key is configured or the lookup failed
    max_credits: int | None = None
    used_credits: int | None = None
    remaining_credits: int | None = None
