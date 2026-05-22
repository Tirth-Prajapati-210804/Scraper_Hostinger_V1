from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol


class ProviderQuotaExhaustedError(RuntimeError):
    pass


class ProviderAuthError(RuntimeError):
    pass


class ProviderRateLimitedError(RuntimeError):
    pass


@dataclass
class ProviderResult:
    """One flight offer from a provider, already normalized."""

    price: float
    currency: str
    airline: str
    deep_link: str
    provider: str = ""
    duration_minutes: int = 0
    stops: int = 0
    raw_data: dict = field(default_factory=dict)


@dataclass
class ProviderSearchDiagnostics:
    """Diagnostic metadata for one provider search attempt."""

    result_reason: str | None = None
    raw_offers_found: int = 0
    eligible_offers_found: int = 0
    visible_results_found: bool = False
    summary_price_found: bool = False
    requested_market: str | None = None
    requested_currency: str | None = None
    detected_currencies: list[str] = field(default_factory=list)
    used_strong_retry: bool = False
    capture_incomplete: bool = False
    rendered_card_count: int = 0
    rendered_captured_count: int = 0
    captured_sorts: list[str] = field(default_factory=list)


@dataclass
class ProviderSearchOutcome:
    """Provider results plus diagnostics used by the collector/logs."""

    results: list[ProviderResult]
    diagnostics: ProviderSearchDiagnostics = field(default_factory=ProviderSearchDiagnostics)


class FlightProvider(Protocol):
    """Protocol that all providers must implement."""

    name: str

    async def search_one_way(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        adults: int = 1,
        cabin: str = "economy",
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> list[ProviderResult]: ...

    async def search_round_trip(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date,
        adults: int = 1,
        cabin: str = "economy",
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> list[ProviderResult]: ...

    async def search_multi_city(
        self,
        legs: list[dict[str, object]],
        adults: int = 1,
        cabin: str = "economy",
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> list[ProviderResult]: ...

    def is_configured(self) -> bool: ...

    async def close(self) -> None: ...
