from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from app.providers.base import ProviderResult
from app.services.export_service import export_route_group
from app.services.price_collector import PriceCollector


def test_multi_city_export_uses_itinerary_sheet_shape() -> None:
    group = SimpleNamespace(
        trip_type="multi_city",
        origins=["YYZ"],
        nights=11,
        sheet_name_map={"YYZ": "Toronto Open Jaw"},
    )

    results = [
        SimpleNamespace(
            origin="YYZ",
            destination="BER",
            depart_date=date(2026, 5, 20),
            airline="Icelandair / Lufthansa",
            price=829.0,
            stops=1,
            stop_label="1 Stop",
            itinerary_data={
                "return_date": "2026-05-31",
                "return_origin": "BUD",
                "outbound_airline": "Icelandair",
                "return_airline": "Lufthansa",
                "stop_result_label": "1 Stop",
                "leg_durations": [500, 620],
            },
        ),
        SimpleNamespace(
            origin="YYZ",
            destination="BER",
            depart_date=date(2026, 5, 21),
            airline="Air Canada / LOT",
            price=851.0,
            stops=2,
            stop_label="2 Stop",
            itinerary_data={
                "return_date": "2026-06-01",
                "return_origin": "BUD",
                "outbound_airline": "Air Canada",
                "return_airline": "LOT",
                "stop_result_label": "2 Stop",
                "leg_durations": [540, 660],
            },
        ),
    ]

    workbook_bytes = export_route_group(group, results)
    workbook = load_workbook(BytesIO(workbook_bytes))

    assert "Toronto Open Jaw" in workbook.sheetnames
    assert workbook.sheetnames == ["Toronto Open Jaw"]

    sheet = workbook["Toronto Open Jaw"]
    headers = [sheet.cell(row=1, column=index).value for index in range(1, 10)]
    assert headers == [
        "Date",
        "Ending Date",
        "Route",
        "Airport",
        "Nights",
        "Airline",
        "Stop Result",
        "Duration",
        "Flight Price",
    ]
    assert sheet["A2"].value == datetime(2026, 5, 20)
    assert sheet["B2"].value == datetime(2026, 5, 31)
    # No per-leg data -> fallback: outbound pair / return-from-home pair.
    assert sheet["C2"].value == "YYZ-BER / BUD-YYZ"
    # Airport = the fare's single airport (stored destination, no actual data).
    assert sheet["D2"].value == "BER"
    assert sheet["E2"].value == 11
    assert sheet["F2"].value == "Icelandair / Lufthansa"
    assert sheet["G2"].value == "1 Stop"
    assert sheet["H2"].value == "8h 20m / 10h 20m"
    assert sheet["I2"].value == 829


def test_multi_city_export_marks_missing_dates_as_na() -> None:
    group = SimpleNamespace(
        trip_type="multi_city",
        origins=["YYZ"],
        nights=11,
        days_ahead=3,
        start_date=date(2026, 5, 20),
        end_date=date(2026, 5, 22),
        sheet_name_map={"YYZ": "Toronto Open Jaw"},
    )

    results = [
        SimpleNamespace(
            origin="YYZ",
            destination="BER",
            depart_date=date(2026, 5, 20),
            airline="Icelandair / Lufthansa",
            price=829.0,
            stops=1,
            stop_label="1 Stop",
            itinerary_data={
                "return_date": "2026-05-31",
                "return_origin": "BUD",
                "outbound_airline": "Icelandair",
                "return_airline": "Lufthansa",
                "stop_result_label": "1 Stop",
                "leg_durations": [500, 620],
            },
        ),
    ]

    workbook_bytes = export_route_group(group, results)
    workbook = load_workbook(BytesIO(workbook_bytes))
    sheet = workbook["Toronto Open Jaw"]

    assert sheet["A2"].value == datetime(2026, 5, 20)
    assert sheet["A3"].value == datetime(2026, 5, 21)
    assert sheet["A4"].value == datetime(2026, 5, 22)
    assert sheet["B3"].value == "N-A"  # Ending Date
    # Columns: Route@C, Airport@D, Nights@E, Airline@F, Stop@G, Duration@H, Price@I.
    assert sheet["D3"].value == "N-A"  # Airport
    assert sheet["F3"].value == "N-A"  # Airline
    assert sheet["G3"].value == "N-A"  # Stop Result
    assert sheet["H3"].value == "N-A"  # Duration
    assert sheet["I3"].value == "N-A"  # Flight Price


def test_multi_city_export_handles_combined_origin_four_leg_itinerary() -> None:
    group = SimpleNamespace(
        trip_type="multi_city",
        origins=["GLA", "PIK"],
        destinations=["KEF"],
        nights=10,
        sheet_name_map={"GLA,PIK": "Glasgow Options"},
        multi_city_legs=[
            SimpleNamespace(origin="KEF", destination="YYZ", nights_before=2),
            SimpleNamespace(origin="NYC", destination="BOS", nights_before=5),
            SimpleNamespace(origin="BOS", destination="", nights_before=3),
        ],
    )

    results = [
        SimpleNamespace(
            origin="GLA,PIK",
            destination="KEF",
            depart_date=date(2026, 7, 1),
            airline="Icelandair / Air Canada",
            price=410.0,
            stops=0,
            stop_label="Direct",
            deep_link="https://example.com/flights",
            itinerary_data={
                "return_date": "2026-07-11",
                "actual_outbound_origin": "GLA",
                "actual_outbound_destination": "KEF",
                "actual_return_origin": "BOS",
                "return_origin": "BOS",
                "legs": [
                    {"actual_origin": "GLA", "actual_destination": "KEF", "duration_minutes": 210},
                    {"actual_origin": "KEF", "actual_destination": "YYZ", "duration_minutes": 360},
                    {"actual_origin": "NYC", "actual_destination": "BOS", "duration_minutes": 80},
                    {"actual_origin": "BOS", "actual_destination": "PIK", "duration_minutes": 390},
                ],
            },
        )
    ]

    workbook_bytes = export_route_group(group, results, include_links=True)
    workbook = load_workbook(BytesIO(workbook_bytes))

    assert "Glasgow Options" in workbook.sheetnames
    sheet = workbook["Glasgow Options"]
    assert sheet["A2"].value == datetime(2026, 7, 1)
    assert sheet["B2"].value == datetime(2026, 7, 11)
    assert sheet["C2"].value == "GLA-KEF / KEF-YYZ / NYC-BOS / BOS-PIK"
    assert sheet["D2"].value == "KEF"
    assert sheet["H2"].value == "3h 30m / 6h 0m / 1h 20m / 6h 30m"
    assert sheet["I2"].value == 410
    assert sheet["J2"].value == "https://example.com/flights"
