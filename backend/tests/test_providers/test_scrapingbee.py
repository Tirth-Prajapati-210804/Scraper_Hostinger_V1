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


def test_rendered_results_scenario_uses_mult_flylocal_url_flow() -> None:
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
    assert "f.settle=f.s" in helper_script
    assert "f.extract=f.e" in helper_script
    assert "o[0]||o[0]" in helper_script
    assert "f.cheap=" in helper_script
    assert instructions[-1] == {"evaluate": "window.FH.extract()"}

    # Same-airline isolation is now carried in the URL (airlines=-MULT,flylocal),
    # so the scenario no longer calls applyFacet(); it only re-asserts the Cheapest
    # sort, then settles (adaptive poll) before extracting.
    evals = [i.get("evaluate") for i in instructions if isinstance(i, dict)]
    assert "window.FH.applyFacet()" not in evals
    assert "window.FH.cheapest()" in evals
    assert "window.FH.settle()" in evals
    assert evals.index("window.FH.cheapest()") < evals.index("window.FH.settle()")
    assert evals.index("window.FH.settle()") < evals.index("window.FH.extract()")
    assert evals.count("window.FH.settle()") >= 2


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


def test_same_airline_and_stop_filter_carried_in_kayak_url() -> None:
    """Same-airline isolation (airlines=-MULT,flylocal) AND the per-leg stop
    filter (stops=...) are carried in the Kayak URL, mirroring Kayak's own UI.
    -MULT alone hid cheaper airlines; flylocal restores them (proven), so both
    tokens are required. No stop token when max_stops >= 2."""
    provider = make_provider()

    url1 = provider._build_search_url(
        origin="MIA", destination="MLA",
        depart_date=date(2026, 6, 5), return_date=date(2026, 6, 18),
        max_stops=1,
    )
    assert url1.endswith("?sort=price_a&fs=airlines=-MULT,flylocal;stops=0,1")

    url0 = provider._build_search_url(
        origin="MIA", destination="MLA",
        depart_date=date(2026, 6, 5), return_date=date(2026, 6, 18),
        max_stops=0,
    )
    assert url0.endswith("?sort=price_a&fs=airlines=-MULT,flylocal;stops=0")

    url2 = provider._build_search_url(
        origin="MIA", destination="MLA",
        depart_date=date(2026, 6, 5), return_date=date(2026, 6, 18),
        max_stops=2,
    )
    # No stop filter when max_stops >= 2, but same-airline isolation still applies.
    assert url2.endswith("?sort=price_a&fs=airlines=-MULT,flylocal")
    assert "stops=" not in url2
    assert "airlines=-MULT,flylocal" in url1 and "airlines=-MULT,flylocal" in url2

    # The scenario no longer clicks the stop facet or applyFacet (URL handles it).
    scenario = provider._build_results_scenario(
        deep=True, same_airline_only=True, minimum_leg_count=2, max_stops=1
    )
    evals = [i.get("evaluate") for i in scenario["instructions"] if isinstance(i, dict)]
    assert not any(e and "window.FH.w(" in e for e in evals)
    assert "window.FH.applyFacet()" not in evals


def _mk_result(price: float) -> ProviderResult:
    return ProviderResult(
        price=price, currency="CAD", airline="X", deep_link="", provider="scrapingbee",
        duration_minutes=100, stops=1, raw_data={},
    )


def _rendered_with_facet(options: list[dict]) -> dict:
    payload = json.dumps({"c": [], "f": {"o": options}})
    return {"evaluate_results": [payload]}


def test_render_missed_cheapest_flags_partial_render() -> None:
    """When our cheapest is clearly above the facet floor, the render missed the
    cheapest airline's cards -> completeness retry should trigger."""
    provider = make_provider()
    rendered = _rendered_with_facet([{"n": "ANA", "p": 2125}, {"n": "United", "p": 2446}])
    # Our cheapest 2446 vs floor 2125 -> missed (> +40 and > +3%).
    assert provider._render_missed_cheapest(rendered, [_mk_result(2446)]) is True


def test_render_missed_cheapest_ignores_when_matching_floor() -> None:
    provider = make_provider()
    rendered = _rendered_with_facet([{"n": "ANA", "p": 2125}])
    # Matches floor -> not missed.
    assert provider._render_missed_cheapest(rendered, [_mk_result(2125)]) is False
    # Within tolerance (+15 < +40) -> not missed.
    assert provider._render_missed_cheapest(rendered, [_mk_result(2140)]) is False


def test_render_missed_cheapest_safe_without_facet_or_results() -> None:
    provider = make_provider()
    # No facet data -> can't judge -> not missed (don't retry blindly).
    assert provider._render_missed_cheapest({"evaluate_results": []}, [_mk_result(2446)]) is False
    # No eligible results -> not missed.
    assert provider._render_missed_cheapest(
        _rendered_with_facet([{"n": "ANA", "p": 2125}]), []
    ) is False


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


def test_same_airline_filter_ignores_airport_pair_route_text() -> None:
    provider = make_provider()
    results = [
        ProviderResult(
            price=901,
            currency="USD",
            airline="Condor",
            deep_link="https://example.com/condor",
            raw_data={
                "airline_names": ["Condor"],
                "legs": [
                    {
                        "airline": "Condor",
                        "route_text": "YYZ-BER",
                    },
                    {
                        "airline": "Condor",
                        "route_text": "BUD-YYZ",
                    },
                ],
                "outbound_airline": "Condor",
                "return_airline": "Condor",
            },
        ),
    ]

    filtered = provider._same_airline_results_only(results)

    assert len(filtered) == 1
    assert filtered[0].airline == "Condor"


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
async def test_round_trip_diagnostic_tries_next_airline_facet_when_first_price_is_suspiciously_high() -> None:
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
                            {"n": "Airline A", "p": 900},
                            {"n": "Airline B", "p": 950},
                        ],
                    },
                }
            )
        ]
    }

    first_result = ProviderResult(
        price=2500,
        currency="USD",
        airline="Airline A",
        deep_link="https://example.com/a",
        raw_data={
            "legs": [
                {"airline": "Airline A", "route_text": "Airline A"},
                {"airline": "Airline A", "route_text": "Airline A"},
            ],
            "leg_stops": [1, 1],
        },
    )
    second_result = ProviderResult(
        price=950,
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
        target_url="https://www.kayak.com/flights/LAS-MLA/2027-03-22/2027-03-30",
        requested_market="us",
        requested_currency="USD",
        market_country_code="us",
        max_stops=1,
        same_airline_only=True,
        minimum_leg_count=2,
    )

    assert calls == [0, 1]
    assert [result.price for result in outcome.results] == [950]
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


@pytest.mark.asyncio
async def test_multi_city_diagnostic_tries_next_airline_facet_when_first_price_is_suspiciously_high() -> None:
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
                            {"n": "Airline A", "p": 900},
                            {"n": "Airline B", "p": 950},
                        ],
                    },
                }
            )
        ]
    }

    first_result = ProviderResult(
        price=2500,
        currency="USD",
        airline="Airline A",
        deep_link="https://example.com/a",
        raw_data={
            "legs": [
                {"airline": "Airline A", "route_text": "Airline A"},
                {"airline": "Airline A", "route_text": "Airline A"},
            ],
            "leg_stops": [1, 1],
        },
    )
    second_result = ProviderResult(
        price=950,
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
                "departure_id": "LAS",
                "arrival_id": "MLA",
                "outbound_date": date(2027, 3, 22),
            },
            {
                "departure_id": "SPU",
                "arrival_id": "LAS",
                "outbound_date": date(2027, 3, 30),
            },
        ],
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=True,
    )

    assert calls == [0, 1]
    assert [result.price for result in results] == [950]
    assert diagnostics.raw_offers_found == 1
    assert diagnostics.eligible_offers_found == 1


@pytest.mark.asyncio
async def test_round_trip_diagnostic_logs_result_diagnostics(monkeypatch) -> None:
    provider = make_provider(REALISTIC_SCRAPINGBEE_KEY)
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

    result = ProviderResult(
        price=700,
        currency="USD",
        airline="Airline A",
        deep_link="https://example.com/a",
        raw_data={
            "legs": [
                {"airline": "Airline A", "route_text": "Airline A"},
                {"airline": "Airline A", "route_text": "Airline A"},
            ],
            "leg_stops": [1, 1],
        },
    )

    async def fake_render_results_attempt(**kwargs):
        return rendered, {}, [result], 1, 1

    provider._render_results_attempt = fake_render_results_attempt

    captured: list[tuple[str, dict[str, object]]] = []

    def fake_info(event, **kwargs):
        captured.append((event, kwargs))

    monkeypatch.setattr("app.providers.scrapingbee.log.info", fake_info)

    target_url = "https://www.kayak.com/flights/DEN-MLA/2027-02-20/2027-03-01"
    await provider._search_rendered_itinerary_diagnostic(
        trip_type="round_trip",
        target_url=target_url,
        requested_market="us",
        requested_currency="USD",
        market_country_code="us",
        max_stops=1,
        same_airline_only=True,
        minimum_leg_count=2,
    )

    events = [kwargs for event, kwargs in captured if event == "scrapingbee_results"]
    assert len(events) == 1
    fields = events[0]
    assert fields["trip_type"] == "round_trip"
    assert fields["target_url"] == target_url
    assert fields["result_reason"] == "success"
    assert fields["raw_offers_found"] == 1
    assert fields["eligible_offers_found"] == 1
    assert fields["selected_facet"] == "Airline A"
    assert fields["facet_option_count"] == 2
    # The diagnostic log must never carry the API key in any field.
    assert REALISTIC_SCRAPINGBEE_KEY not in json.dumps(fields)


# ---------------------------------------------------------------------------
# Baseline-locking tests (Stage 2): pin current behavior of pure helpers
# before any scraper logic change. These are characterization tests — if one
# fails after an intended change, decide deliberately whether the new behavior
# is correct, do not "fix" the test reflexively.
# ---------------------------------------------------------------------------


def _round_trip_result(price: float, leg_stops: list[int], airline: str = "Air Canada") -> ProviderResult:
    return ProviderResult(
        price=price,
        currency="USD",
        airline=airline,
        deep_link="https://example.com/x",
        raw_data={
            "legs": [
                {"airline": airline, "route_text": airline}
                for _ in leg_stops
            ],
            "leg_stops": list(leg_stops),
        },
    )


def test_filter_by_stops_rejects_result_when_any_leg_exceeds_limit() -> None:
    """max_stops is per leg: a 0/2 itinerary must be rejected at max_stops=1
    even though the outbound leg is fine."""
    provider = make_provider()
    results = [
        _round_trip_result(700, [1, 1]),   # both legs within limit -> keep
        _round_trip_result(650, [0, 2]),   # return leg has 2 stops -> drop
        _round_trip_result(680, [2, 0]),   # outbound leg has 2 stops -> drop
    ]

    filtered = provider._filter_results_by_stops(results, max_stops=1)

    assert [r.price for r in filtered] == [700]


def test_filter_by_stops_nonstop_only_rejects_any_stop() -> None:
    provider = make_provider()
    results = [
        _round_trip_result(700, [0, 0]),
        _round_trip_result(650, [0, 1]),
    ]

    filtered = provider._filter_results_by_stops(results, max_stops=0)

    assert [r.price for r in filtered] == [700]


def test_filter_by_stops_none_limit_keeps_everything() -> None:
    provider = make_provider()
    results = [
        _round_trip_result(700, [0, 0]),
        _round_trip_result(650, [3, 4]),
    ]

    filtered = provider._filter_results_by_stops(results, max_stops=None)

    assert {r.price for r in filtered} == {700, 650}


def test_normalize_rejects_non_flight_transport_card() -> None:
    """A leg that is a train/bus must not be normalized into a flight result."""
    provider = make_provider()
    results = provider._normalize_rendered_cards(
        {
            "cards": [
                {
                    "text": "Deutsche Bahn $120",
                    "price_text": "$120",
                    "airline_text": "Deutsche Bahn",
                    "legs": [
                        {"airline": "Deutsche Bahn", "route_text": "Train to airport", "duration_text": "2h"},
                        {"airline": "Deutsche Bahn", "route_text": "Train", "duration_text": "2h"},
                    ],
                },
                {
                    "text": "Lufthansa $480",
                    "price_text": "$480",
                    "airline_text": "Lufthansa",
                    "legs": [
                        {"airline": "Lufthansa", "route_text": "Lufthansa", "duration_text": "10h", "stops_text": "1 stop"},
                        {"airline": "Lufthansa", "route_text": "Lufthansa", "duration_text": "11h", "stops_text": "1 stop"},
                    ],
                },
            ]
        },
        currency="USD",
        deep_link="https://www.kayak.com/flights/FRA-JFK/2027-03-12/2027-03-20",
        trip_type="round_trip",
        market_country_code="us",
        expected_leg_count=2,
    )

    assert [r.airline for r in results] == ["Lufthansa"]


def test_should_probe_alternate_facets_when_price_clearly_above_floor() -> None:
    """Stale-facet guard: a result >=20% AND >=$150 above the cheapest facet
    floor should trigger probing other airline facets."""
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps({"c": [], "f": {"s": "A", "o": [{"n": "A", "p": 900}, {"n": "B", "p": 950}]}})
        ]
    }
    eligible = [_round_trip_result(1200, [1, 1])]  # 1200 >= 900*1.2 (1080) and >= 900+150 (1050)

    assert provider._should_probe_alternate_airline_facets(
        rendered=rendered,
        eligible_results=eligible,
        facet_option_count=2,
    ) is True


def test_should_not_probe_alternate_facets_when_price_near_floor() -> None:
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps({"c": [], "f": {"s": "A", "o": [{"n": "A", "p": 900}, {"n": "B", "p": 950}]}})
        ]
    }
    eligible = [_round_trip_result(1000, [1, 1])]  # below both clearly-high thresholds, and < 1500

    assert provider._should_probe_alternate_airline_facets(
        rendered=rendered,
        eligible_results=eligible,
        facet_option_count=2,
    ) is False


def test_should_not_probe_alternate_facets_with_single_option() -> None:
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps({"c": [], "f": {"s": "A", "o": [{"n": "A", "p": 900}]}})
        ]
    }
    eligible = [_round_trip_result(5000, [1, 1])]

    assert provider._should_probe_alternate_airline_facets(
        rendered=rendered,
        eligible_results=eligible,
        facet_option_count=1,
    ) is False


def test_extract_rendered_cards_payload_decodes_short_keys() -> None:
    """The minified js_scenario emits short keys (c/f/s/n/m). The decoder must
    map them back to the verbose structure the normalizer expects."""
    provider = make_provider()
    payload = provider._extract_rendered_cards_payload(
        {
            "evaluate_results": [
                json.dumps(
                    {
                        "n": 7,
                        "m": 3,
                        "c": [
                            {
                                "t": "Air France $676",
                                "p": "$676",
                                "a": "Air France",
                                "l": [
                                    {"a": "Air France", "s": "1 stop", "d": "10h"},
                                    {"a": "Air France", "s": "1 stop", "d": "11h"},
                                ],
                            }
                        ],
                        "s": {"c": "$676", "b": "$700", "q": "$900"},
                        "f": {"s": "Air France", "o": [{"n": "Air France", "p": 676}]},
                    }
                )
            ]
        }
    )

    assert payload is not None
    assert payload["card_count"] == 7
    assert payload["captured_count"] == 3
    assert len(payload["cards"]) == 1
    assert payload["cards"][0]["price_text"] == "$676"
    assert payload["cards"][0]["legs"][0]["airline"] == "Air France"
    assert payload["summary"]["cheapest"] == "$676"
    assert payload["facet"]["selected"] == "Air France"
    assert payload["facet"]["options"][0]["name"] == "Air France"


def test_render_failure_snapshot_detects_selector_presence_and_block() -> None:
    """The failure snapshot must surface drift-vs-hydration signals without
    leaking full HTML or secrets."""
    provider = make_provider(REALISTIC_SCRAPINGBEE_KEY)

    hydrated_but_drifted = {
        "initial-status-code": 200,
        "cost": 25,
        "resolved-url": "https://www.kayak.com/flights/BOS-EDI/2026-07-01/2026-07-10",
        "evaluate_results": ["{}"],
        # Result containers present, but the price class is absent -> drift signal.
        "body": "<html><title>Cheap Flights BOS to EDI | KAYAK</title>"
                "<div class='nrc6'><ol class='hJSA-list'></ol></div>Airlines</html>",
    }
    snap = provider._render_failure_snapshot(hydrated_but_drifted)
    assert snap["http_status"] == 200
    assert snap["cost"] == 25
    assert snap["title"] == "Cheap Flights BOS to EDI | KAYAK"
    assert snap["markers"]["card_cls"] is True
    assert snap["markers"]["leg_list_cls"] is True
    assert snap["markers"]["airlines_facet"] is True
    assert snap["markers"]["price_cls"] is False  # the drift fingerprint
    assert snap["markers"]["captcha_or_block"] is False

    blocked = {
        "initial-status-code": 200,
        "body": "<html><title>Robot Check</title>Please verify you are not a robot. CAPTCHA</html>",
    }
    snap2 = provider._render_failure_snapshot(blocked)
    assert snap2["markers"]["captcha_or_block"] is True
    assert snap2["markers"]["price_cls"] is False

    # Safety: the snapshot must never carry the full body or any API key.
    big_body = "x" * 50000
    snap3 = provider._render_failure_snapshot({"body": big_body})
    serialized = json.dumps(snap3)
    assert big_body not in serialized
    assert snap3["body_length"] == 50000
    assert REALISTIC_SCRAPINGBEE_KEY not in serialized


def test_render_budget_stays_below_client_timeout() -> None:
    """The ScrapingBee render budget must always leave headroom under the httpx
    client timeout, so a slow-but-valid render returns before httpx aborts."""
    # Production value.
    provider = ScrapingBeeProvider(api_key="k", timeout=120)
    budget_ms = provider._render_budget_ms()
    assert budget_ms == 85_000  # (120 - 35) * 1000
    assert budget_ms < provider._timeout * 1000  # strictly below client timeout
    # Headroom is at least 30s.
    assert provider._timeout * 1000 - budget_ms >= 30_000

    # Old default still safe.
    assert ScrapingBeeProvider(api_key="k", timeout=90)._render_budget_ms() == 55_000

    # Never exceeds ScrapingBee's hard cap even with a very large client timeout.
    assert ScrapingBeeProvider(api_key="k", timeout=600)._render_budget_ms() == 140_000

    # Never collapses below a sane floor for tiny timeouts.
    assert ScrapingBeeProvider(api_key="k", timeout=10)._render_budget_ms() == 20_000


def test_base_request_params_uses_decoupled_render_budget() -> None:
    provider = ScrapingBeeProvider(api_key="k", timeout=120)
    params = provider._base_request_params("https://www.kayak.com/flights/MIA-MLA/2026-06-05")
    assert params["timeout"] == 85_000


def test_payload_decodes_no_results_flag() -> None:
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps({"c": [], "f": {"s": "", "o": []}, "np": True})
        ]
    }
    payload = provider._extract_rendered_cards_payload(rendered)
    assert payload is not None
    assert payload["no_results"] is True
    assert provider._rendered_payload_reports_no_results(rendered) is True


def test_payload_no_results_defaults_false_when_absent() -> None:
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps({"c": [], "f": {"s": "A", "o": [{"n": "A", "p": 700}]}})
        ]
    }
    assert provider._rendered_payload_reports_no_results(rendered) is False


def test_no_results_helper_emitted_in_scenario() -> None:
    """The empty-route detector must be present in the rendered scenario so
    legitimately empty Kayak routes can be told apart from failed renders."""
    provider = make_provider()
    scenario = provider._build_results_scenario(
        deep=True, same_airline_only=True, minimum_leg_count=2
    )
    helper_script = scenario["instructions"][0]["evaluate"]
    assert "f.empty=" in helper_script
    assert "np:f.empty()" in helper_script


def test_accuracy_audit_reports_saved_vs_floor_gap() -> None:
    """The audit must expose how far the saved fare sits above Kayak's cheapest
    visible airline-facet price, so accuracy drift is measurable from logs."""
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps(
                {
                    "c": [],
                    "f": {
                        "s": "Icelandair",
                        "o": [
                            {"n": "Icelandair", "p": 579},
                            {"n": "Scandinavian Airlines", "p": 669},
                            {"n": "Air France", "p": 735},
                        ],
                    },
                }
            )
        ]
    }
    eligible = [_round_trip_result(579, [1, 1], airline="Icelandair")]

    audit = provider._accuracy_audit(
        rendered=rendered,
        summary_prices={"cheapest": "$560", "best": "$640"},
        eligible_results=eligible,
    )

    assert audit["saved_price"] == 579
    assert audit["facet_floor"] == 579  # cheapest visible facet
    assert audit["summary_cheapest"] == 560  # Kayak headline (may be mixed-airline)
    assert audit["floor_gap"] == 0.0  # saved == cheapest same-airline facet -> ideal
    assert audit["summary_gap"] == 19.0  # 579 - 560, expected when headline is mixed


def test_accuracy_audit_flags_saved_above_facet_floor() -> None:
    """If the saved fare is well above the cheapest visible facet, floor_gap is
    positive -> a real accuracy red flag (scraper kept a worse same-airline card)."""
    provider = make_provider()
    rendered = {
        "evaluate_results": [
            json.dumps(
                {
                    "c": [],
                    "f": {"s": "Delta", "o": [{"n": "Icelandair", "p": 579}, {"n": "Delta", "p": 735}]},
                }
            )
        ]
    }
    eligible = [_round_trip_result(735, [1, 1], airline="Delta")]

    audit = provider._accuracy_audit(
        rendered=rendered,
        summary_prices={},
        eligible_results=eligible,
    )

    assert audit["facet_floor"] == 579
    assert audit["saved_price"] == 735
    assert audit["floor_gap"] == 156.0  # 735 - 579 -> saved is $156 above the floor
    assert audit["summary_cheapest"] is None
    assert audit["summary_gap"] is None


def test_route_airport_pair_parses_actual_airports() -> None:
    provider = make_provider()
    assert provider._route_airport_pair("FCO-IAD") == ("FCO", "IAD")
    assert provider._route_airport_pair("IAD - EDI") == ("IAD", "EDI")
    assert provider._route_airport_pair("fco–iad") == ("FCO", "IAD")  # en-dash, lowercase
    assert provider._route_airport_pair("Aer Lingus") is None
    assert provider._route_airport_pair("") is None


def test_normalize_surfaces_actual_airport_when_city_code_searched() -> None:
    """When a group searches a city code (ROM), the saved data must expose the
    actual airport Kayak returned (FCO), parsed from the leg route text."""
    provider = make_provider()
    results = provider._normalize_rendered_cards(
        {
            "cards": [
                {
                    "text": "Aer Lingus $1051",
                    "price_text": "$1,051",
                    "airline_text": "Aer Lingus",
                    "legs": [
                        {
                            "airline": "Aer Lingus",
                            "route_text": "IAD-EDI",
                            "stops_text": "1 stop",
                            "duration_text": "14h 15m",
                        },
                        {
                            "airline": "Aer Lingus",
                            "route_text": "FCO-IAD",
                            "stops_text": "1 stop",
                            "duration_text": "14h 05m",
                        },
                    ],
                }
            ]
        },
        currency="USD",
        deep_link="https://www.kayak.com/flights/IAD-EDI/2027-04-14/ROM-IAD/2027-04-24",
        trip_type="multi_city",
        market_country_code="us",
        expected_leg_count=2,
    )

    assert len(results) == 1
    raw = results[0].raw_data
    assert raw["actual_outbound_origin"] == "IAD"
    assert raw["actual_outbound_destination"] == "EDI"
    assert raw["actual_return_origin"] == "FCO"  # the real Rome airport, not ROM
    assert raw["actual_return_destination"] == "IAD"
    assert raw["legs"][1]["actual_origin"] == "FCO"
    assert provider._actual_route_label(results) == "IAD->EDI / FCO->IAD"
