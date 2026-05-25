from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

import pytest

from app.providers.base import ProviderResult
from app.providers.scrapingbee import (
    _RESULT_PRICE_SELECTOR,
    _SAME_AIRLINE_INITIAL_WAIT_MS,
    ScrapingBeeProvider,
)

REALISTIC_SCRAPINGBEE_KEY = "x" * 88


def make_provider(api_key: str = "test-key") -> ScrapingBeeProvider:
    return ScrapingBeeProvider(
        api_key=api_key,
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
    assert instructions[2] == {"wait": _SAME_AIRLINE_INITIAL_WAIT_MS}
    assert "f.applyFacet=f.a" in helper_script
    assert "f.w=x=>" in helper_script
    assert "f.settle=f.s" in helper_script
    assert "f.extract=f.e" in helper_script
    assert "o[0]||o[0]" in helper_script
    assert {"wait": 1600} in instructions
    assert any(
        instruction.get("evaluate") == "window.FH.applyFacet()"
        for instruction in instructions
        if isinstance(instruction, dict)
    )
    assert instructions[-1] == {"evaluate": "window.FH.extract()"}


def test_rendered_results_scenario_can_select_later_airline_facet() -> None:
    provider = make_provider()

    scenario = provider._build_results_scenario(
        deep=True,
        same_airline_only=True,
        minimum_leg_count=2,
        airline_facet_index=2,
    )

    helper_script = scenario["instructions"][0]["evaluate"]
    assert "o[2]||o[0]" in helper_script


def test_rendered_results_scenario_can_apply_stop_prefilter() -> None:
    provider = make_provider()

    scenario = provider._build_results_scenario(
        deep=True,
        same_airline_only=True,
        minimum_leg_count=2,
        max_stops=1,
    )

    instructions = scenario["instructions"]
    assert {"evaluate": "window.FH.w(1)"} in instructions


def test_round_trip_rendered_request_stays_under_request_line_cap() -> None:
    provider = make_provider(REALISTIC_SCRAPINGBEE_KEY)
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
            max_stops=1,
        ),
        separators=(",", ":"),
    )
    params["block_resources"] = "True"

    assert params["wait"] == 0
    assert params["wait_browser"] == "load"
    assert len(urlencode(params)) < 8190


def test_multi_city_rendered_request_stays_under_request_line_cap() -> None:
    provider = make_provider(REALISTIC_SCRAPINGBEE_KEY)
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
            max_stops=1,
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


def test_same_airline_filter_rejects_mixed_leg_operator_text() -> None:
    provider = make_provider()
    results = [
        ProviderResult(
            price=1218,
            currency="USD",
            airline="Air Canada",
            deep_link="https://example.com/mixed",
            raw_data={
                "legs": [
                    {
                        "airline": "Air Canada",
                        "route_text": "Air Canada, SWISS",
                    },
                    {
                        "airline": "Air Canada",
                        "route_text": "Air Canada",
                    },
                ],
            },
        ),
        ProviderResult(
            price=1260,
            currency="USD",
            airline="British Airways",
            deep_link="https://example.com/same",
            raw_data={
                "legs": [
                    {
                        "airline": "British Airways",
                        "route_text": "British Airways",
                    },
                    {
                        "airline": "British Airways",
                        "route_text": "British Airways",
                    },
                ],
            },
        ),
    ]

    filtered = provider._same_airline_results_only(results)

    assert len(filtered) == 1
    assert filtered[0].airline == "British Airways"


def test_rendered_card_normalization_records_final_settled_price() -> None:
    provider = make_provider()
    results = provider._normalize_rendered_cards(
        {
            "cards": [
                {
                    "text": "Air France $676",
                    "price_text": "$676",
                    "initial_price_text": "$677",
                    "airline_text": "Air France",
                    "legs": [
                        {
                            "airline": "Air France",
                            "route_text": "Air France",
                            "stops_text": "1 stop",
                            "duration_text": "10h 00m",
                        },
                        {
                            "airline": "Air France",
                            "route_text": "Air France",
                            "stops_text": "1 stop",
                            "duration_text": "11h 00m",
                        },
                    ],
                }
            ]
        },
        currency="USD",
        deep_link="https://www.kayak.com/flights/EWR-MLA/2027-03-12/2027-03-20",
        trip_type="round_trip",
        market_country_code="us",
        expected_leg_count=2,
    )

    assert len(results) == 1
    assert results[0].price == 676
    assert results[0].raw_data["initial_price"] == 677
    assert results[0].raw_data["final_price"] == 676
    assert results[0].raw_data["price_adjusted_after_settle"] is True


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


@pytest.mark.asyncio
async def test_round_trip_diagnostic_tries_next_airline_facet_when_first_fails_stops() -> None:
    provider = make_provider()
    calls: list[int] = []
    rendered = {
        "evaluate_results": [
            json.dumps(
                {
                    "c": [],
                    "f": {
                        "s": "Airline A",
                        "o": [
                            {"n": "Airline A", "p": 700},
                            {"n": "Airline B", "p": 760},
                        ],
                    },
                }
            )
        ]
    }

    first_result = ProviderResult(
        price=700,
        currency="USD",
        airline="Airline A",
        deep_link="https://example.com/a",
        raw_data={
            "legs": [
                {"airline": "Airline A", "route_text": "Airline A"},
                {"airline": "Airline A", "route_text": "Airline A"},
            ],
            "leg_stops": [2, 2],
        },
    )
    second_result = ProviderResult(
        price=760,
        currency="USD",
        airline="Airline B",
        deep_link="https://example.com/b",
        raw_data={
            "legs": [
                {"airline": "Airline B", "route_text": "Airline B"},
                {"airline": "Airline B", "route_text": "Airline B"},
            ],
            "leg_stops": [1, 1],
        },
    )

    async def fake_render_results_attempt(**kwargs):
        calls.append(int(kwargs.get("airline_facet_index", 0)))
        if len(calls) == 1:
            return rendered, {}, [first_result], 1, 1
        return rendered, {}, [second_result], 1, 1

    provider._render_results_attempt = fake_render_results_attempt

    outcome = await provider._search_rendered_itinerary_diagnostic(
        trip_type="round_trip",
        target_url="https://www.kayak.com/flights/DEN-MLA/2027-02-20/2027-03-01",
        requested_market="us",
        requested_currency="USD",
        market_country_code="us",
        max_stops=1,
        same_airline_only=True,
        minimum_leg_count=2,
    )

    assert calls == [0, 1]
    assert [result.price for result in outcome.results] == [760]
    assert outcome.diagnostics.raw_offers_found == 1
    assert outcome.diagnostics.eligible_offers_found == 1


@pytest.mark.asyncio
async def test_multi_city_diagnostic_tries_next_airline_facet_when_first_fails_stops() -> None:
    provider = make_provider()
    calls: list[int] = []
    rendered = {
        "evaluate_results": [
            json.dumps(
                {
                    "c": [],
                    "f": {
                        "s": "Airline A",
                        "o": [
                            {"n": "Airline A", "p": 700},
                            {"n": "Airline B", "p": 760},
                        ],
                    },
                }
            )
        ]
    }

    first_result = ProviderResult(
        price=700,
        currency="USD",
        airline="Airline A",
        deep_link="https://example.com/a",
        raw_data={
            "legs": [
                {"airline": "Airline A", "route_text": "Airline A"},
                {"airline": "Airline A", "route_text": "Airline A"},
            ],
            "leg_stops": [2, 2],
        },
    )
    second_result = ProviderResult(
        price=760,
        currency="USD",
        airline="Airline B",
        deep_link="https://example.com/b",
        raw_data={
            "legs": [
                {"airline": "Airline B", "route_text": "Airline B"},
                {"airline": "Airline B", "route_text": "Airline B"},
            ],
            "leg_stops": [1, 1],
        },
    )

    async def fake_render_results_attempt(**kwargs):
        calls.append(int(kwargs.get("airline_facet_index", 0)))
        if len(calls) == 1:
            return rendered, {}, [first_result], 1, 1
        return rendered, {}, [second_result], 1, 1

    provider._render_results_attempt = fake_render_results_attempt

    results, diagnostics = await provider._search_multi_city_once(
        legs=[
            {
                "departure_id": "DEN",
                "arrival_id": "MLA",
                "outbound_date": date(2027, 2, 20),
            },
            {
                "departure_id": "SPU",
                "arrival_id": "DEN",
                "outbound_date": date(2027, 3, 1),
            },
        ],
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=True,
    )

    assert calls == [0, 1]
    assert [result.price for result in results] == [760]
    assert diagnostics.raw_offers_found == 1
    assert diagnostics.eligible_offers_found == 1
