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


def test_route_group_accepts_max_leg_duration() -> None:
    payload = RouteGroupCreate(
        name="Canada to Japan",
        destination_label="Japan",
        destinations=["NRT"],
        origins=["YVR"],
        max_leg_duration_minutes=720,
    )

    assert payload.max_leg_duration_minutes == 720
