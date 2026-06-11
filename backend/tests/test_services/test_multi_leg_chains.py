"""3/4-leg multi-city support: date chains, schema rules, segments, URLs.

Client-validated semantics: nights_before = nights BETWEEN legs, an exact
day offset (LON-KEF 01 Jul + 2 -> 03 Jul; +5 -> 08 Jul), minimum 1 (next-day
departure). Middle legs may be open-jaw (arrive HAN, later depart SAI).
3/4-leg Kayak URLs probed live 2026-06-10: render + parse identical to 2-leg.
"""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.providers.scrapingbee import ScrapingBeeProvider
from app.schemas.route_group import MultiCityLeg, RouteGroupCreate
from app.services.price_collector import _build_multi_city_legs
from app.utils.route_segments import ExtraLeg, iter_group_segments


class _Group:
    def __init__(self, **kw):
        self.trip_type = kw.get("trip_type", "multi_city")
        self.origins = kw.get("origins", ["YHZ"])
        self.destinations = kw.get("destinations", ["HAN"])
        self.nights = kw.get("nights", 9)
        self.special_sheets = kw.get("special_sheets", [])
        self.multi_city_legs = kw.get("multi_city_legs")


def make_provider() -> ScrapingBeeProvider:
    return ScrapingBeeProvider(api_key="test-key", timeout=90)


def test_three_leg_chain_dates_match_client_example() -> None:
    """The client's own example (journey-builder screenshot 2026-06-10):
    LON->KEF 01 Jul (2 nights) -> KEF->YYZ 03 Jul (2 nights Toronto + 3 nights
    New York = 5) -> NYC->LON 08 Jul. nights_before = EXACT day offset."""
    legs = _build_multi_city_legs(
        origin="LON",
        destination="KEF",
        depart_date=date(2026, 7, 1),
        extra_legs=[
            ExtraLeg(origin="KEF", destination="YYZ", nights_before=2),
            ExtraLeg(origin="NYC", destination="", nights_before=5),
        ],
        nights=None,
        return_origin=None,
    )
    assert [
        (leg["departure_id"], leg["arrival_id"], leg["outbound_date"]) for leg in legs
    ] == [
        ("LON", "KEF", date(2026, 7, 1)),
        ("KEF", "YYZ", date(2026, 7, 3)),
        ("NYC", "LON", date(2026, 7, 8)),  # "" destination -> back to origin
    ]


def test_legacy_two_leg_chain_unchanged() -> None:
    legs = _build_multi_city_legs(
        origin="YEG",
        destination="HAN",
        depart_date=date(2026, 7, 2),
        extra_legs=None,
        nights=14,
        return_origin="SAI",
    )
    assert legs == [
        {"departure_id": "YEG", "arrival_id": "HAN", "outbound_date": date(2026, 7, 2)},
        {"departure_id": "SAI", "arrival_id": "YEG", "outbound_date": date(2026, 7, 17)},
    ]


def test_chain_url_builder_three_and_four_legs() -> None:
    provider = make_provider()
    legs = [
        {"departure_id": "YHZ", "arrival_id": "HAN", "outbound_date": date(2026, 10, 4)},
        {"departure_id": "SAI", "arrival_id": "YYC", "outbound_date": date(2026, 10, 14)},
        {"departure_id": "YYC", "arrival_id": "YHZ", "outbound_date": date(2026, 10, 15)},
    ]
    url = provider._build_multi_city_chain_url(
        legs=legs, market="ca", currency="CAD", max_stops=2, same_airline=True
    )
    assert "/flights/YHZ-HAN/2026-10-04/SAI-YYC/2026-10-14/YYC-YHZ/2026-10-15" in url
    assert "airlines=-MULT,flylocal" in url and "baditin=baditin" in url

    # Toggle OFF (client rule: only add -MULT when same-airline is ON).
    url_off = provider._build_multi_city_chain_url(
        legs=legs, market="ca", currency="CAD", max_stops=2, same_airline=False
    )
    assert "-MULT" not in url_off and "flylocal" not in url_off
    assert "baditin=baditin" in url_off  # longer-flights stay visible either way

    four = legs + [
        {"departure_id": "YHZ", "arrival_id": "YYZ", "outbound_date": date(2026, 10, 20)}
    ]
    url4 = provider._build_multi_city_chain_url(legs=four, market="ca", currency="CAD")
    assert url4.count("/2026-") == 4


@pytest.mark.asyncio
async def test_multi_city_search_rejects_wrong_leg_counts() -> None:
    provider = make_provider()
    with pytest.raises(ValueError):
        await provider._search_multi_city_once(
            legs=[{"departure_id": "A", "arrival_id": "B", "outbound_date": date(2026, 1, 1)}],
        )
    with pytest.raises(ValueError):
        await provider._search_multi_city_once(
            legs=[
                {"departure_id": "A", "arrival_id": "B", "outbound_date": date(2026, 1, 1)}
            ] * 5,
        )


def test_segments_built_from_multi_city_legs() -> None:
    group = _Group(
        origins=["YHZ", "YEG"],
        destinations=["HAN"],
        multi_city_legs=[
            {"origin": "SAI", "destination": "YYC", "nights_before": 9},
            {"origin": "YYC", "destination": "", "nights_before": 1},
        ],
    )
    segments = iter_group_segments(group)
    assert len(segments) == 2
    assert segments[0].extra_legs == [
        ExtraLeg(origin="SAI", destination="YYC", nights_before=9),
        ExtraLeg(origin="YYC", destination="", nights_before=1),
    ]
    # return_origin (labels/export) = where the homebound leg departs.
    assert segments[0].return_origin == "YYC"


def test_segments_legacy_fallback_from_special_sheets() -> None:
    group = _Group(
        special_sheets=[{"origin": "SAI", "destinations": ["YHZ"]}],
        nights=14,
    )
    segments = iter_group_segments(group)
    # Legacy rule is depart + nights + 1, so under exact-day-offset semantics
    # the synthesized leg is nights+1 -- existing groups keep identical dates.
    assert segments[0].extra_legs == [
        ExtraLeg(origin="SAI", destination="", nights_before=15)
    ]
    assert segments[0].return_origin == "SAI"


def test_schema_rejects_bad_leg_configs() -> None:
    base = dict(
        name="x", destination_label="y", destinations=["HAN"], origins=["YHZ"],
        trip_type="multi_city",
    )
    # empty destination on a NON-last leg
    with pytest.raises(ValidationError):
        RouteGroupCreate(
            **base,
            multi_city_legs=[
                MultiCityLeg(origin="SAI", destination="", nights_before=3),
                MultiCityLeg(origin="YYC", destination="", nights_before=1),
            ],
        )
    # too many extra legs (max 3 -> 4 total)
    with pytest.raises(ValidationError):
        RouteGroupCreate(
            **base,
            multi_city_legs=[
                MultiCityLeg(origin="AAA", destination="BBB", nights_before=1)
            ] * 4,
        )
    # legs on a round_trip group
    with pytest.raises(ValidationError):
        RouteGroupCreate(
            name="x", destination_label="y", destinations=["HAN"], origins=["YHZ"],
            trip_type="round_trip",
            multi_city_legs=[MultiCityLeg(origin="SAI", destination="", nights_before=3)],
        )
    # valid: 2 extra legs, last one returns home, multi_city without special_sheets
    group = RouteGroupCreate(
        **base,
        multi_city_legs=[
            MultiCityLeg(origin="SAI", destination="YYC", nights_before=9),
            MultiCityLeg(origin="YYC", destination="", nights_before=1),
        ],
    )
    assert group.multi_city_legs is not None and len(group.multi_city_legs) == 2
