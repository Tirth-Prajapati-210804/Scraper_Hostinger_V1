from __future__ import annotations

from app.utils.location_resolver import search_location_suggestions


def test_search_location_suggestions_matches_prefixes() -> None:
    results = search_location_suggestions("cal", limit=25)
    labels = {item["label"] for item in results}
    city_labels = {item["label"] for item in results if item["kind"] == "city"}

    assert "Calgary" in labels
    assert "Calgary" in city_labels


def test_search_location_suggestions_includes_airport_codes() -> None:
    results = search_location_suggestions("cun", limit=10)

    assert any(item["kind"] == "airport_code" and "CUN" in item["codes"] for item in results)
