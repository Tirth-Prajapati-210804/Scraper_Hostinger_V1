from __future__ import annotations

import uuid
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import MagicMock

import openpyxl
import pytest

from app.services.export_service import export_route_group


# ── helpers ──────────────────────────────────────────────────────────────────

def make_route_group(
    sheet_name_map: dict | None = None,
    special_sheets: list | None = None,
    destination_label: str = "SGN",
    nights: int = 7,
    destinations: list | None = None,
) -> MagicMock:
    rg = MagicMock()
    rg.id = uuid.uuid4()
    rg.name = "Test Group"
    rg.destination_label = destination_label
    rg.destinations = destinations if destinations is not None else ["SGN"]
    rg.nights = nights
    rg.sheet_name_map = sheet_name_map or {"YVR": "YVR"}
    rg.special_sheets = special_sheets or []
    rg.trip_type = "round_trip"
    return rg


def make_result(
    origin: str = "YVR",
    destination: str = "SGN",
    depart_date: date | None = None,
    price: float = 200.0,
    airline: str = "VJ",
) -> MagicMock:
    r = MagicMock()
    r.origin = origin
    r.destination = destination
    r.depart_date = depart_date or (date.today() + timedelta(days=1))
    r.price = price
    r.airline = airline
    r.stop_label = ""
    r.stops = 1
    r.duration_minutes = 120
    r.itinerary_data = None
    return r


# ── tests ─────────────────────────────────────────────────────────────────────

def test_export_creates_one_sheet_per_origin() -> None:
    rg = make_route_group(sheet_name_map={"YVR": "Vancouver", "YYZ": "Toronto"})
    results = [make_result(origin="YVR"), make_result(origin="YYZ")]
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, results)))
    assert "Vancouver" in wb.sheetnames
    assert "Toronto" in wb.sheetnames


def test_export_sanitizes_invalid_and_duplicate_sheet_names() -> None:
    rg = make_route_group(sheet_name_map={"YVR": "A/B", "YYZ": "A:B"})
    results = [make_result(origin="YVR"), make_result(origin="YYZ")]
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, results)))
    assert "A-B" in wb.sheetnames
    assert "A-B-2" in wb.sheetnames


def test_export_has_correct_headers() -> None:
    rg = make_route_group()
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [make_result()])))
    ws = wb["YVR"]
    assert ws.cell(1, 1).value == "Date"
    assert ws.cell(1, 2).value == "Return Date"
    assert ws.cell(1, 3).value == "Route"
    assert ws.cell(1, 4).value == "Airport"
    assert ws.cell(1, 5).value == "Nights"
    assert ws.cell(1, 6).value == "Airline"
    assert ws.cell(1, 7).value == "Stop Result"
    assert ws.cell(1, 8).value == "Duration"
    assert ws.cell(1, 9).value == "Flight Price"
    assert ws.cell(2, 1).number_format == "DD-MM-YYYY"
    # Return Date = depart + nights, also a real date cell.
    assert ws.cell(2, 2).number_format == "DD-MM-YYYY"


def test_export_route_column_shows_round_trip_path() -> None:
    # Round-trip Route is one cell: ORIGIN-DEST-ORIGIN (e.g. YVR-SGN-YVR). The
    # destination uses the actual flown code; the journey label is the filename.
    rg = make_route_group(destination_label="Saigon", destinations=["SGN"])
    result = make_result(origin="YVR", destination="SGN")
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YVR"]
    assert ws.cell(2, 3).value == "YVR-SGN-YVR"


def test_export_route_column_annotates_searched_metro_code() -> None:
    # When a metro code is searched but a specific airport is flown, the Route
    # shows "ACTUAL (SEARCHED)" for that hop -- e.g. searched ROM, flew FCO.
    rg = make_route_group(destination_label="London - Rome", destinations=["ROM"])
    result = make_result(origin="YVR", destination="ROM")
    result.itinerary_data = {"actual_outbound_destination": "FCO"}
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YVR"]
    assert ws.cell(2, 3).value == "YVR-FCO (ROM)-YVR"


def test_export_nights_in_night_column() -> None:
    rg = make_route_group(nights=12)
    result = make_result()
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YVR"]
    # Nights is column 5 (Return Date @2, Route @3, Airport @4).
    assert ws.cell(2, 5).value == 12


def test_export_return_date_is_depart_plus_nights() -> None:
    rg = make_route_group(nights=3)
    d = date.today() + timedelta(days=1)
    result = make_result(depart_date=d)
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YVR"]
    assert ws.cell(2, 2).value.date() == d + timedelta(days=3)


def test_export_prices_are_integers() -> None:
    rg = make_route_group()
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [make_result(price=199.75)])))
    ws = wb["YVR"]
    assert ws.cell(2, 8).value == "2h 0m"
    assert ws.cell(2, 9).value == 200


def test_export_cheapest_per_date() -> None:
    rg = make_route_group()
    today = date.today()
    d = today + timedelta(days=1)
    results = [
        make_result(origin="YVR", destination="SGN", depart_date=d, price=500.0, airline="XX"),
        make_result(origin="YVR", destination="HAN", depart_date=d, price=300.0, airline="VN"),
    ]
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, results)))
    ws = wb["YVR"]
    assert ws.cell(2, 9).value == 300
    assert ws.cell(2, 6).value == "VN"


def test_export_missing_date_shows_none_price() -> None:
    rg = make_route_group(sheet_name_map={"YVR": "YVR", "YYZ": "Toronto"})
    today = date.today()
    results = [make_result(origin="YVR", depart_date=today + timedelta(days=1), price=100.0)]
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, results)))
    ws = wb["Toronto"]
    # Airport @4 then Airline/Stop/Duration/Price at 6-9 (Route @3, Nights @5).
    assert ws.cell(2, 4).value == "N-A"
    assert ws.cell(2, 6).value == "N-A"
    assert ws.cell(2, 7).value == "N-A"
    assert ws.cell(2, 8).value == "N-A"
    assert ws.cell(2, 9).value == "N-A"


def test_export_special_sheet_4_columns() -> None:
    special = {
        "name": "Special",
        "origin": "YVR",
        "destinations": ["SGN"],
        "destination_label": "SGN",
        "columns": 4,
    }
    rg = make_route_group(special_sheets=[special])
    result = make_result(origin="YVR", destination="SGN", price=250.0)
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["Special"]
    assert ws.cell(1, 1).value == "Date"
    assert ws.cell(1, 2).value == "Dep Airport"
    assert ws.cell(1, 3).value == "Arrival Airport"
    assert ws.cell(1, 4).value == "Flight Price"
    assert ws.cell(2, 4).value == 250


def test_export_special_sheet_6_columns() -> None:
    special = {
        "name": "Multi",
        "origin": "YVR",
        "destinations": ["SGN", "HAN"],
        "destination_label": "VN",
        "columns": 6,
    }
    rg = make_route_group(nights=7, special_sheets=[special])
    result = make_result(origin="YVR", destination="SGN", price=400.0, airline="VN")
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["Multi"]
    assert ws.cell(1, 4).value == "Nights"
    assert ws.cell(1, 5).value == "Airline"
    assert ws.cell(1, 6).value == "Stop Result"
    assert ws.cell(1, 7).value == "Duration"
    assert ws.cell(1, 8).value == "Flight Price"
    assert ws.cell(2, 4).value == 7
    assert ws.cell(2, 5).value == "VN"
    assert ws.cell(2, 8).value == 400


def test_export_uses_per_leg_duration_label_when_available() -> None:
    rg = make_route_group()
    result = make_result()
    result.duration_minutes = 2175
    result.itinerary_data = {"leg_durations": [1450, 725]}

    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YVR"]

    # Duration is column 8 (Return Date @2, Route @3, Airport @4, Nights @5).
    assert ws.cell(2, 8).value == "24h 10m / 12h 5m"


def test_multi_city_export_compares_destination_alternatives_per_origin_date() -> None:
    rg = make_route_group(sheet_name_map={"YOW": "YOW"})
    rg.trip_type = "multi_city"
    rg.origins = ["YOW"]

    first = make_result(origin="YOW", destination="LHR", price=671.0, airline="Air France")
    first.itinerary_data = {
        "return_date": "2026-06-13",
        "return_origin": "MXP",
    }

    second = make_result(origin="YOW", destination="LGW", price=702.0, airline="KLM")
    second.itinerary_data = {
        "return_date": "2026-06-13",
        "return_origin": "MXP",
    }

    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [first, second])))

    assert wb.sheetnames == ["YOW"]
    # Multi-city now has one Route column (col 3) instead of Dep/Arrival/Return From.
    assert wb["YOW"].cell(1, 3).value == "Route"
    assert wb["YOW"].cell(1, 4).value == "Airport"
    assert wb["YOW"].cell(1, 7).value == "Stop Result"
    assert wb["YOW"].cell(1, 8).value == "Duration"
    assert wb["YOW"].cell(1, 9).value == "Flight Price"
    # No per-leg data here -> fallback: outbound pair / return-from-home pair,
    # joined by ' / ' (the open-jaw gap, not a continuous chain).
    assert wb["YOW"].cell(2, 3).value == "YOW-LHR / MXP-YOW"
    assert wb["YOW"].cell(2, 9).value == 671
    assert wb["YOW"].cell(2, 1).number_format == "DD-MM-YYYY"
    assert wb["YOW"].cell(2, 2).number_format == "DD-MM-YYYY"


def test_multi_city_route_uses_per_leg_pairs() -> None:
    # When per-leg airports are present, the Route is each flight leg as a FROM-TO
    # pair joined by ' / ' (open-jaw: pairs do NOT chain). 3 flights here.
    rg = make_route_group(sheet_name_map={"YVR": "YVR"})
    rg.trip_type = "multi_city"
    rg.origins = ["YVR"]
    result = make_result(origin="YVR", destination="BER", price=1500.0)
    result.itinerary_data = {
        "return_date": "2026-07-12",
        "legs": [
            {"actual_origin": "YVR", "actual_destination": "BER"},
            {"actual_origin": "BER", "actual_destination": "LON"},
            {"actual_origin": "BUD", "actual_destination": "YVR"},
        ],
    }
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YVR"]
    assert ws.cell(2, 3).value == "YVR-BER / BER-LON / BUD-YVR"


def test_na_row_gets_search_link_with_swapped_dates() -> None:
    # With include_links, an N-A (no-fare) row still gets a clickable verify link:
    # a sibling result's stable search URL with THIS row's dates swapped in (same
    # scraper filters). The collected date keeps its own link.
    rg = make_route_group(sheet_name_map={"YVR": "YVR"}, nights=3)
    rg.days_ahead = 3
    rg.start_date = date(2026, 7, 1)
    rg.end_date = date(2026, 7, 3)
    result = make_result(origin="YVR", destination="SGN", depart_date=date(2026, 7, 1))
    result.deep_link = "https://www.kayak.com/flights/YVR-SGN/2026-07-01/2026-07-04?sort=price_a&fs=stops=0"
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result], include_links=True)))
    ws = wb["YVR"]
    # Row 2 (collected) keeps its real link; rows 3-4 (N-A) get date-swapped links.
    assert ws.cell(2, 10).value == result.deep_link
    assert ws.cell(3, 10).value == "https://www.kayak.com/flights/YVR-SGN/2026-07-02/2026-07-05?sort=price_a&fs=stops=0"
    assert ws.cell(4, 10).value == "https://www.kayak.com/flights/YVR-SGN/2026-07-03/2026-07-06?sort=price_a&fs=stops=0"


def test_na_row_link_is_na_when_no_template_exists() -> None:
    # If NO date on the route ever collected a link, N-A rows stay N-A (nothing to
    # build a template from).
    rg = make_route_group(sheet_name_map={"YVR": "YVR", "YYZ": "Toronto"}, nights=3)
    rg.days_ahead = 2
    rg.start_date = date(2026, 7, 1)
    rg.end_date = date(2026, 7, 2)
    result = make_result(origin="YVR", depart_date=date(2026, 7, 1))
    result.deep_link = "https://www.kayak.com/flights/YVR-SGN/2026-07-01/2026-07-04"
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result], include_links=True)))
    # Toronto sheet has no collected results at all -> N-A link.
    ws = wb["Toronto"]
    assert ws.cell(2, 10).value == "N-A"


def test_multi_city_route_annotates_searched_metro_per_leg() -> None:
    # Actual airports flown are annotated with the SEARCHED metro code at the same
    # leg position, e.g. searched TYO/SEL but flew NRT/ICN -> NRT (TYO) / ICN (SEL).
    rg = make_route_group(sheet_name_map={"YEG": "YEG"})
    rg.trip_type = "multi_city"
    rg.origins = ["YEG"]
    rg.multi_city_legs = [{"origin": "SEL", "destination": "", "nights_before": 9}]
    result = make_result(origin="YEG", destination="TYO", price=2139.0)
    result.itinerary_data = {
        "return_date": "2026-07-10",
        "legs": [
            {"actual_origin": "YEG", "actual_destination": "NRT"},
            {"actual_origin": "ICN", "actual_destination": "YEG"},
        ],
    }
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))
    ws = wb["YEG"]
    assert ws.cell(2, 3).value == "YEG-NRT (TYO) / ICN (SEL)-YEG"


def test_multi_city_export_sanitizes_invalid_sheet_names() -> None:
    rg = make_route_group(sheet_name_map={"YOW": "YOW/Canada"})
    rg.trip_type = "multi_city"
    rg.origins = ["YOW"]
    result = make_result(origin="YOW", destination="TIA", price=671.0, airline="Air France")
    result.itinerary_data = {
        "return_date": "2026-06-13",
        "return_origin": "SPU",
    }

    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [result])))

    assert "YOW-Canada" in wb.sheetnames


def test_export_airport_column_shows_fare_airport_for_combined_destination() -> None:
    # Combined multi-airport rows are stored under "ORY,CDG" -- the Airport column
    # tells the client WHICH airport the fare is actually for. Prefers the actual
    # extracted arrival airport; N-A when a combined row has no actual airport.
    rg = make_route_group(destination_label="Paris", destinations=["ORY", "CDG"])
    with_actual = make_result(origin="YVR", destination="ORY,CDG")
    with_actual.itinerary_data = {"actual_outbound_destination": "CDG"}
    d2 = date.today() + timedelta(days=2)
    without_actual = make_result(origin="YVR", destination="ORY,CDG", depart_date=d2)
    wb = openpyxl.load_workbook(BytesIO(export_route_group(rg, [with_actual, without_actual])))
    ws = wb["YVR"]
    assert ws.cell(1, 4).value == "Airport"
    assert ws.cell(2, 4).value == "CDG"
    assert ws.cell(3, 4).value == "N-A"
