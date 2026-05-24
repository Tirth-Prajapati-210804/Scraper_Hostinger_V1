from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

import pytest

from app.providers.base import ProviderResult
from app.providers.scrapingbee import (
    _RESULT_PRICE_SELECTOR,
    ScrapingBeeProvider,
)


def make_provider() -> ScrapingBeeProvider:
    return ScrapingBeeProvider(
        api_key="test-key",
        timeout=90,
        max_retries=1,
        concurrency_limit=2,
        rendered_concurrency_limit=1,
    )


def test_rendered_results_scenario_uses_facet_primary_flow() -> None:
    provider = make_provider()

    scenario = provider._build_results_scenario(
        deep=True,
        same_airline_only=True,
        minimum_leg_count=2,
    )

    instructions = scenario["instructions"]
    helper_script = instructions[0]["evaluate"]

    assert instructions[1] == {"wait_for": _RESULT_PRICE_SELECTOR}
    assert "f.applyFacet=f.a" in helper_script
    assert "f.settle=f.s" in helper_script
    assert "f.extract=f.e" in helper_script
    assert any(
        instruction.get("evaluate") == "window.__fhCollector.applyFacet()"
        for instruction in instructions
        if isinstance(instruction, dict)
    )
    assert instructions[-1] == {"evaluate": "window.__fhCollector.extract()"}


def test_round_trip_rendered_request_stays_under_request_line_cap() -> None:
    provider = make_provider()
    target_url = provider._build_search_url(
        origin="MIA",
        destination="MLA",
        depart_date=date(2026, 6, 5),
        return_date=date(2026, 6, 18),
        market="us",
        currency="USD",
    )
    params = provider._base_request_params(target_url, country_code="us")
    params["json_response"] = "True"
    params["js_scenario"] = json.dumps(
        provider._build_results_scenario(
            deep=True,
            same_airline_only=True,
            minimum_leg_count=2,
        ),
        separators=(",", ":"),
    )
    params["block_resources"] = "True"

    assert params["wait"] == 0
    assert params["wait_browser"] == "load"
    assert len(urlencode(params)) < 8190


def test_multi_city_rendered_request_stays_under_request_line_cap() -> None:
    provider = make_provider()
    target_url = provider._build_multi_city_results_url(
        outbound_origin="YYZ",
        outbound_destination="TIA",
        outbound_date=date(2026, 5, 29),
        inbound_origin="SPU",
        inbound_destination="YYZ",
        inbound_date=date(2026, 6, 12),
        market="ca",
        currency="CAD",
    )
    params = provider._base_request_params(target_url, country_code="ca")
    params["json_response"] = "True"
    params["js_scenario"] = json.dumps(
        provider._build_results_scenario(
            deep=True,
            same_airline_only=True,
            minimum_leg_count=2,
        ),
        separators=(",", ":"),
    )
    params["block_resources"] = "True"

    assert len(urlencode(params)) < 8190


def test_same_airline_filter_keeps_single_airline_aliases_only() -> None:
    provider = make_provider()
    results = [
        ProviderResult(
            price=1000,
            currency="USD",
            airline="Air Canada / AC",
            deep_link="https://example.com/a",
            raw_data={"outbound_airline": "Air Canada", "return_airline": "AC"},
        ),
        ProviderResult(
            price=1100,
            currency="USD",
            airline="Air Canada / Lufthansa",
            deep_link="https://example.com/b",
            raw_data={"outbound_airline": "Air Canada", "return_airline": "Lufthansa"},
        ),
    ]

    filtered = provider._same_airline_results_only(results)

    assert len(filtered) == 1
    assert filtered[0].airline == "Air Canada"


@pytest.mark.asyncio
async def test_round_trip_diagnostic_forces_same_airline_without_unbound_local() -> None:
    provider = make_provider()
    captured: dict[str, object] = {}

    async def fake_search_rendered_itinerary_diagnostic(**kwargs):
        captured.update(kwargs)
        return []

    provider._search_rendered_itinerary_diagnostic = fake_search_rendered_itinerary_diagnostic

    await provider.search_round_trip_diagnostic(
        origin="MIA",
        destination="MLA",
        depart_date=date(2026, 6, 5),
        return_date=date(2026, 6, 18),
        market="us",
        currency="USD",
        same_airline_only=False,
    )

    assert captured["trip_type"] == "round_trip"
    assert captured["same_airline_only"] is True
    assert captured["minimum_leg_count"] == 2
