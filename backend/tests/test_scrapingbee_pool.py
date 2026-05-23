from __future__ import annotations

from datetime import date

import pytest

from app.providers.base import (
    ProviderQuotaExhaustedError,
    ProviderResult,
    ProviderSearchDiagnostics,
    ProviderSearchOutcome,
)
from app.providers.scrapingbee import ScrapingBeePoolProvider


@pytest.mark.asyncio
async def test_scrapingbee_pool_falls_back_to_next_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.providers import scrapingbee as scrapingbee_module

    class DummyProvider:
        name = "scrapingbee"

        def __init__(self, api_key: str, **_: object) -> None:
            self.api_key = api_key

        def is_configured(self) -> bool:
            return True

        async def search_one_way(self, **_: object) -> list[ProviderResult]:
            if self.api_key == "key-one":
                raise ProviderQuotaExhaustedError("quota exhausted")

            return [
                ProviderResult(
                    price=123.0,
                    currency="USD",
                    airline="AC",
                    deep_link="https://example.com",
                    provider="scrapingbee",
                )
            ]

        async def search_round_trip(self, **_: object) -> list[ProviderResult]:
            return await self.search_one_way()

        async def search_multi_city(self, **_: object) -> list[ProviderResult]:
            return await self.search_one_way()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(scrapingbee_module, "ScrapingBeeProvider", DummyProvider)

    provider = ScrapingBeePoolProvider(
        api_keys=["key-one", "key-two", "key-three"],
    )

    results = await provider.search_one_way(
        origin="YYZ",
        destination="DPS",
        depart_date=date(2026, 6, 1),
    )

    assert len(results) == 1
    assert results[0].price == 123.0


@pytest.mark.asyncio
async def test_scrapingbee_pool_exposes_diagnostic_search_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.providers import scrapingbee as scrapingbee_module

    class DummyProvider:
        name = "scrapingbee"

        def __init__(self, api_key: str, **_: object) -> None:
            self.api_key = api_key

        def is_configured(self) -> bool:
            return True

        async def search_round_trip_diagnostic(self, **_: object) -> ProviderSearchOutcome:
            if self.api_key == "key-one":
                raise ProviderQuotaExhaustedError("quota exhausted")

            return ProviderSearchOutcome(
                results=[
                    ProviderResult(
                        price=456.0,
                        currency="USD",
                        airline="AC",
                        deep_link="https://example.com/rt",
                        provider="scrapingbee",
                    )
                ],
                diagnostics=ProviderSearchDiagnostics(
                    result_reason="success",
                    raw_offers_found=1,
                    eligible_offers_found=1,
                ),
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(scrapingbee_module, "ScrapingBeeProvider", DummyProvider)

    provider = ScrapingBeePoolProvider(
        api_keys=["key-one", "key-two"],
    )

    outcome = await provider.search_round_trip_diagnostic(
        origin="YYZ",
        destination="DPS",
        depart_date=date(2026, 6, 1),
        return_date=date(2026, 6, 11),
    )

    assert len(outcome.results) == 1
    assert outcome.results[0].price == 456.0
    assert outcome.diagnostics.result_reason == "success"
