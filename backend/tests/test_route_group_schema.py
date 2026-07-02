from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.route_group import RouteGroupCreate


def test_route_group_create_normalizes_codes_and_currency() -> None:
    payload = RouteGroupCreate(
        name=" Canada to Japan ",
        destination_label=" Japan ",
        destinations=["nrt", "hnd"],
        origins=["yvr"],
        nights=10,
        days_ahead=30,
        market="CA",
        currency="usd",
    )

    assert payload.name == "Canada to Japan"
    assert payload.destination_label == "Japan"
    assert payload.destinations == ["NRT", "HND"]
    assert payload.origins == ["YVR"]
    assert payload.market == "ca"
    assert payload.currency == "USD"


def test_route_group_rejects_invalid_currency() -> None:
    with pytest.raises(ValidationError):
        RouteGroupCreate(
            name="Bad currency",
            destination_label="Japan",
            destinations=["NRT"],
            origins=["YVR"],
            nights=7,
            days_ahead=30,
            currency="USDX",
        )


def test_route_group_accepts_two_letter_market() -> None:
    payload = RouteGroupCreate(
        name="India market",
        destination_label="Japan",
        destinations=["NRT"],
        origins=["YVR"],
        nights=7,
        days_ahead=30,
        market="IN",
        currency="USD",
    )

    assert payload.market == "in"


def test_route_group_rejects_invalid_market() -> None:
    with pytest.raises(ValidationError):
        RouteGroupCreate(
            name="Bad market",
            destination_label="Japan",
            destinations=["NRT"],
            origins=["YVR"],
            nights=7,
            days_ahead=30,
            market="india",
            currency="USD",
        )


def test_route_group_rejects_invalid_date_range() -> None:
    with pytest.raises(ValidationError):
        RouteGroupCreate(
            name="Canada to Japan",
            destination_label="Japan",
            destinations=["NRT"],
            origins=["YVR"],
            start_date=date(2026, 5, 10),
            end_date=date(2026, 5, 1),
        )


def test_route_group_accepts_exact_two_stop_mode() -> None:
    payload = RouteGroupCreate(
        name="Canada to Japan",
        destination_label="Japan",
        destinations=["NRT"],
        origins=["YVR"],
        max_stops=2,
    )

    assert payload.max_stops == 2


def test_route_group_accepts_same_airline_only_flag() -> None:
    payload = RouteGroupCreate(
        name="Canada to Japan",
        destination_label="Japan",
        destinations=["NRT"],
        origins=["YVR"],
        trip_type="round_trip",
        same_airline_only=True,
    )

    assert payload.same_airline_only is True


def test_route_group_defaults_same_airline_only_to_true() -> None:
    payload = RouteGroupCreate(
        name="Canada to Japan",
        destination_label="Japan",
        destinations=["NRT"],
        origins=["YVR"],
    )

    assert payload.same_airline_only is True


def test_route_group_rejects_one_way_trip_type() -> None:
    with pytest.raises(ValidationError, match="trip_type must be one of"):
        RouteGroupCreate(
            name="Canada to Japan",
            destination_label="Japan",
            destinations=["NRT"],
            origins=["YVR"],
            trip_type="one_way",
        )


def test_route_group_accepts_max_leg_duration() -> None:
    payload = RouteGroupCreate(
        name="Canada to Japan",
        destination_label="Japan",
        destinations=["NRT"],
        origins=["YVR"],
        max_leg_duration_minutes=720,
    )

    assert payload.max_leg_duration_minutes == 720


def test_multi_city_leg_accepts_comma_joined_alternatives() -> None:
    # A leg's From/To may list ALTERNATIVE airports ("asj, ses" -> "ASJ,SES");
    # the collector searches every combination and saves only the cheapest.
    payload = RouteGroupCreate(
        name="Korea open jaw",
        destination_label="Korea",
        destinations=["ICN"],
        origins=["YVR"],
        nights=10,
        days_ahead=30,
        trip_type="multi_city",
        multi_city_legs=[{"origin": "asj, ses", "destination": "", "nights_before": 5}],
    )
    assert payload.multi_city_legs is not None
    assert payload.multi_city_legs[0].origin == "ASJ,SES"

    with pytest.raises(ValidationError):
        RouteGroupCreate(
            name="Bad leg code",
            destination_label="Korea",
            destinations=["ICN"],
            origins=["YVR"],
            nights=10,
            days_ahead=30,
            trip_type="multi_city",
            multi_city_legs=[{"origin": "ASJ SES", "destination": "", "nights_before": 5}],
        )


def test_multi_city_leg_combination_cap_rejects_credit_bombs() -> None:
    # 5 leg-1 destinations x 8 leg-2 origins = 40 combos passes; adding one more
    # leg-1 destination (48) must be rejected -- each combo is a paid search.
    def make(destinations: list[str]):
        return RouteGroupCreate(
            name="Combo cap",
            destination_label="Korea",
            destinations=destinations,
            origins=["YVR"],
            nights=10,
            days_ahead=30,
            trip_type="multi_city",
            multi_city_legs=[
                {
                    "origin": "AAA,BBB,CCC,DDD,EEE,FFF,GGG,HHH",
                    "destination": "",
                    "nights_before": 5,
                }
            ],
        )

    make(["ICN", "GMP", "PUS", "CJU", "KAZ"])  # 40 -> allowed
    with pytest.raises(ValidationError):
        make(["ICN", "GMP", "PUS", "CJU", "KAZ", "TAE"])  # 48 -> rejected
