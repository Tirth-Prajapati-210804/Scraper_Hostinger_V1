from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.providers.base import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderResult,
)
from app.providers.scrapedo import ScrapeDoPoolProvider, ScrapeDoProvider


def _uk_round_trip_poll_payload(*, status: str = "complete", price: float = 1188.0) -> dict:
    return {
        "status": status,
        "searchId": "search-uk-1",
        "totalCount": 1,
        "filteredCount": 1,
        "airlines": {
            "AF": {"name": "Air France"},
        },
        "legs": {
            "leg-out": {
                "duration": 745,
                "segments": [{"id": "1797399600000AF01", "layover": {"duration": 180}}],
            },
            "leg-in": {
                "duration": 1060,
                "segments": [{"id": "1798263600000AF02", "layover": {"duration": 240}}],
            },
        },
        "segments": {
            "1797399600000AF01": {
                "airline": "AF",
                "origin": "BHX",
                "destination": "JRO",
                "equipmentTypeName": "Airbus A350",
            },
            "1798263600000AF02": {
                "airline": "AF",
                "origin": "JRO",
                "destination": "BHX",
                "equipmentTypeName": "Airbus A350",
            },
        },
        "results": [
            {
                "type": "inlineAd",
                "bookingOptions": [{"displayPrice": {"price": 1, "currency": "GBP"}}],
                "legs": [],
            },
            {
                "type": "core",
                "bookingOptions": [
                    {
                        "providerCode": "AIRFRANCE",
                        "displayPrice": {"price": price, "currency": "GBP"},
                        "bookingUrl": {"url": "/book/flight?id=af1188"},
                    }
                ],
                "legs": [{"id": "leg-out"}, {"id": "leg-in"}],
            },
        ],
    }


@pytest.mark.asyncio
async def test_round_trip_two_call_poll_flow_and_geocode() -> None:
    calls: list[tuple[dict[str, str], dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((dict(request.url.params), json.loads(request.content)))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={"status": "partial", "searchId": "search-uk-1", "results": []},
                headers={"Scrape.do-Request-Cost": "10", "Scrape.do-Remaining-Credits": "9300"},
            )
        return httpx.Response(
            200,
            json=_uk_round_trip_poll_payload(status="complete", price=1188.0),
            headers={"Scrape.do-Request-Cost": "10", "Scrape.do-Remaining-Credits": "9290"},
        )

    provider = ScrapeDoProvider(
        api_key="sd-test-key",
        initial_poll_delay_seconds=0.0,
        poll_delay_seconds=0.01,
        max_polls=3,
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        outcome = await provider.search_round_trip_diagnostic(
            origin="BHX",
            destination="JRO",
            depart_date=date(2026, 12, 16),
            return_date=date(2026, 12, 26),
            market="uk",
            currency="GBP",
            max_stops=1,
            same_airline_only=True,
            max_layover_minutes=900,
        )
    finally:
        await provider.close()

    assert len(calls) == 2
    assert calls[0][0]["token"] == "sd-test-key"
    assert calls[0][0]["url"] == "https://www.kayak.co.uk/i/api/search/dynamic/flights/poll"
    assert calls[0][0]["super"] == "true"
    assert calls[0][0]["geoCode"] == "gb"
    assert "render" not in calls[0][0]
    assert "searchId" not in calls[0][1]["userSearchParams"]
    assert calls[1][1]["userSearchParams"]["searchId"] == "search-uk-1"
    expected_fs = "airlines=-MULT,flylocal;baditin=baditin;stops=0,1;layoverdur=-900"
    assert calls[1][1]["filterParams"]["fs"] == expected_fs

    assert len(outcome.results) == 1
    res = outcome.results[0]
    assert res.price == 1188.0
    assert res.currency == "GBP"
    assert res.airline == "Air France"
    assert res.provider == "scrapedo"
    assert res.duration_minutes == 1805
    assert res.stops == 0
    assert res.deep_link.startswith("https://www.kayak.co.uk/flights/BHX-JRO/2026-12-16/2026-12-26")
    assert res.raw_data["actual_outbound_origin"] == "BHX"
    assert res.raw_data["actual_outbound_destination"] == "JRO"
    assert res.raw_data["actual_return_destination"] == "BHX"
    assert res.raw_data["poll_credits_cost"] == 20.0
    assert provider._last_remaining_credits == 9290
    assert outcome.diagnostics.result_reason == "success"


@pytest.mark.asyncio
async def test_ca_multi_airport_splits_codes_and_sets_geoca() -> None:
    calls: list[tuple[dict[str, str], dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((dict(request.url.params), json.loads(request.content)))
        payload = {
            "status": "complete",
            "searchId": "search-ca-1",
            "totalCount": 1,
            "airlines": {"TS": {"name": "Air Transat"}},
            "legs": {
                "l1": {"duration": 440, "segments": ["1797399600000TS01"]},
                "l2": {"duration": 495, "segments": ["1798263600000TS02"]},
            },
            "segments": {
                "1797399600000TS01": {"airline": "TS", "origin": "YYZ", "destination": "ORY"},
                "1798263600000TS02": {"airline": "TS", "origin": "ORY", "destination": "YYZ"},
            },
            "results": [
                {
                    "type": "core",
                    "bookingOptions": [{"displayPrice": {"price": 640, "currency": "CAD"}}],
                    "legs": [{"id": "l1"}, {"id": "l2"}],
                }
            ],
        }
        return httpx.Response(200, json=payload, headers={"Scrape.do-Request-Cost": "10"})

    provider = ScrapeDoProvider(api_key="sd-key", initial_poll_delay_seconds=0.0)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        outcome = await provider.search_round_trip_diagnostic(
            origin="YYZ,YTZ",
            destination="CDG,ORY",
            depart_date=date(2027, 3, 12),
            return_date=date(2027, 3, 19),
            market="ca",
            currency="CAD",
            max_stops=1,
            same_airline_only=True,
        )
    finally:
        await provider.close()

    # Start already returned status=complete + results -> 1 call (10 credits).
    assert len(calls) == 1
    assert calls[0][0]["url"] == "https://www.ca.kayak.com/i/api/search/dynamic/flights/poll"
    assert calls[0][0]["geoCode"] == "ca"
    assert calls[0][1]["userSearchParams"]["legs"][0]["origin"]["airports"] == ["YYZ", "YTZ"]
    assert calls[0][1]["userSearchParams"]["legs"][0]["destination"]["airports"] == ["CDG", "ORY"]
    assert outcome.results[0].raw_data["actual_outbound_destination"] == "ORY"
    assert outcome.results[0].raw_data["poll_credits_cost"] == 10.0


@pytest.mark.asyncio
async def test_multi_city_chain_builds_legs_and_raw_annotations() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "searchId": "mc-1",
                "totalCount": 1,
                "airlines": {"AC": {"name": "Air Canada"}},
                "legs": {
                    "l1": {"duration": 520, "segments": ["1797399600000AC01"]},
                    "l2": {"duration": 120, "segments": ["1797486000000AC02", "1797489600000AC03"]},
                    "l3": {"duration": 600, "segments": ["1797572400000AC04"]},
                },
                "segments": {
                    "1797399600000AC01": {"airline": "AC", "origin": "YVR", "destination": "BER"},
                    "1797486000000AC02": {"airline": "AC", "origin": "BER", "destination": "FRA"},
                    "1797489600000AC03": {"airline": "AC", "origin": "FRA", "destination": "LON"},
                    "1797572400000AC04": {"airline": "AC", "origin": "BUD", "destination": "YVR"},
                },
                "results": [
                    {
                        "type": "core",
                        "bookingOptions": [{"displayPrice": {"price": 1250, "currency": "CAD"}}],
                        "legs": [{"id": "l1"}, {"id": "l2"}, {"id": "l3"}],
                    }
                ],
            },
        )

    provider = ScrapeDoProvider(api_key="sd-key", initial_poll_delay_seconds=0.0)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        outcome = await provider.search_multi_city_diagnostic(
            legs=[
                {"departure_id": "YVR", "arrival_id": "BER", "outbound_date": date(2027, 7, 1)},
                {"departure_id": "BER", "arrival_id": "LON", "outbound_date": date(2027, 7, 4)},
                {"departure_id": "BUD", "arrival_id": "YVR", "outbound_date": date(2027, 7, 9)},
            ],
            market="ca",
            currency="CAD",
            max_stops=1,
            same_airline_only=True,
        )
    finally:
        await provider.close()

    assert len(calls[0]["userSearchParams"]["legs"]) == 3
    res = outcome.results[0]
    assert res.raw_data["trip_type"] == "multi_city"
    assert res.raw_data["stop_result_label"] == "Direct / 1 Stop / Direct"
    assert res.raw_data["return_origin"] == "BUD"
    assert res.raw_data["return_date"] == "2027-07-09"


@pytest.mark.asyncio
async def test_filters_non_flight_rail_and_excessive_layover_as_filtered_out() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "searchId": "rail-1",
                "totalCount": 2,
                "airlines": {"9B": {"name": "AccessRail"}, "LH": {"name": "Lufthansa"}},
                "legs": {
                    "l1": {"duration": 300, "segments": ["17973996000009B01"]},
                    "l2": {"duration": 300, "segments": ["1797486000000LH01"]},
                    "l3": {
                        "duration": 900,
                        "segments": ["1797399600000LH02", "1797430000000LH03"],
                        "layovers": [{"duration": 950}],
                    },
                },
                "segments": {
                    "17973996000009B01": {"airline": "9B", "origin": "LON", "destination": "PAR"},
                    "1797486000000LH01": {"airline": "LH", "origin": "PAR", "destination": "LON"},
                    "1797399600000LH02": {"airline": "LH", "origin": "LON", "destination": "FRA"},
                    "1797430000000LH03": {"airline": "LH", "origin": "FRA", "destination": "JRO"},
                },
                "results": [
                    {
                        "type": "core",
                        "bookingOptions": [{"displayPrice": {"price": 150, "currency": "GBP"}}],
                        "legs": [{"id": "l1"}, {"id": "l2"}],
                    },
                    {
                        "type": "core",
                        "bookingOptions": [{"displayPrice": {"price": 800, "currency": "GBP"}}],
                        "legs": [{"id": "l3"}, {"id": "l2"}],
                    },
                ],
            },
        )

    provider = ScrapeDoProvider(api_key="sd-key", initial_poll_delay_seconds=0.0)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        outcome = await provider.search_round_trip_diagnostic(
            origin="LON",
            destination="JRO",
            depart_date=date(2027, 5, 1),
            return_date=date(2027, 5, 10),
            market="uk",
            currency="GBP",
            max_stops=1,
            same_airline_only=True,
            max_layover_minutes=660,
        )
    finally:
        await provider.close()

    assert outcome.results == []
    assert outcome.diagnostics.result_reason == "filtered_out"


@pytest.mark.asyncio
async def test_mult_zero_eligible_fallback_reuses_same_search_id() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "airlines=-MULT" in body.get("filterParams", {}).get("fs", ""):
            return httpx.Response(
                200,
                json={"status": "complete", "searchId": "reuse-99", "totalCount": 0, "results": []},
                headers={"Scrape.do-Request-Cost": "10"},
            )
        return httpx.Response(
            200,
            json=_uk_round_trip_poll_payload(status="complete", price=999.0),
            headers={"Scrape.do-Request-Cost": "10"},
        )

    provider = ScrapeDoProvider(api_key="sd-key", initial_poll_delay_seconds=0.0)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        outcome = await provider.search_round_trip_diagnostic(
            origin="BHX",
            destination="JRO",
            depart_date=date(2027, 6, 1),
            return_date=date(2027, 6, 11),
            market="uk",
            currency="GBP",
            max_stops=1,
            same_airline_only=True,
        )
    finally:
        await provider.close()

    assert len(calls) == 2
    assert calls[1]["userSearchParams"]["searchId"] == "reuse-99"
    assert "airlines=-MULT" not in calls[1]["filterParams"]["fs"]
    assert outcome.diagnostics.used_strong_retry is True
    assert outcome.results[0].price == 999.0
    assert outcome.results[0].raw_data["poll_credits_cost"] == 20.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "exc_cls"),
    [
        (402, {"message": "No credits left"}, ProviderQuotaExhaustedError),
        (401, {"message": "Invalid token"}, ProviderAuthError),
        (429, {"message": "Too many requests"}, ProviderRateLimitedError),
        (200, "<html>Please confirm that you are a real KAYAK user</html>", ProviderBlockedError),
    ],
)
async def test_error_classifications(
    status_code: int, body: object, exc_cls: type[Exception]
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(status_code, json=body)
        return httpx.Response(status_code, text=str(body))

    provider = ScrapeDoProvider(api_key="sd-key", max_retries=1)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(exc_cls):
            await provider.search_round_trip_diagnostic(
                origin="BHX",
                destination="JRO",
                depart_date=date(2027, 6, 1),
                return_date=date(2027, 6, 11),
            )
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_pool_provider_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.providers import scrapedo as scrapedo_module

    class DummyProvider:
        def __init__(self, api_key: str, **kwargs):
            self.api_key = api_key

        def is_configured(self) -> bool:
            return True

        async def search_round_trip_diagnostic(self, **kwargs):
            if self.api_key == "bad-key":
                raise ProviderQuotaExhaustedError("out")
            return [
                ProviderResult(
                    price=500.0,
                    currency="GBP",
                    airline="KLM",
                    deep_link="https://k",
                    provider="scrapedo",
                )
            ]

        async def close(self) -> None:
            return None

    monkeypatch.setattr(scrapedo_module, "ScrapeDoProvider", DummyProvider)
    pool = ScrapeDoPoolProvider(api_keys=["bad-key", "good-key"])
    try:
        outcome = await pool.search_round_trip_diagnostic(
            origin="MAN",
            destination="DXB",
            depart_date=date(2027, 6, 1),
            return_date=date(2027, 6, 8),
        )
    finally:
        await pool.close()

    assert [item.price for item in outcome.results] == [500.0]
