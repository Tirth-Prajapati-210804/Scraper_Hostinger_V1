from __future__ import annotations

import pytest

from app.utils.location_resolver import _codes_for_entry, search_location_suggestions


def _city_suggestion_codes(query: str) -> list[str] | None:
    for item in search_location_suggestions(query, limit=8):
        if item["kind"] == "city" and item["label"].casefold() == query.casefold():
            return item["codes"]
    return None


def test_search_location_suggestions_matches_prefixes() -> None:
    results = search_location_suggestions("cal", limit=25)
    labels = {item["label"] for item in results}
    city_labels = {item["label"] for item in results if item["kind"] == "city"}

    assert "Calgary" in labels
    assert "Calgary" in city_labels


def test_search_location_suggestions_includes_airport_codes() -> None:
    results = search_location_suggestions("cun", limit=10)

    assert any(item["kind"] == "airport_code" and "CUN" in item["codes"] for item in results)


@pytest.mark.parametrize(
    "city,metro",
    [
        # Only kayak-verified metro codes (docs/kayak_metro_city_codes_seed.csv).
        ("Rome", "ROM"),
        ("New York", "NYC"),
        ("Tokyo", "TYO"),
        ("Paris", "PAR"),
        ("London", "LON"),
        ("Milan", "MIL"),
        ("Washington, D.C.", "WAS"),
        ("Buenos Aires", "BUE"),
        ("Chicago", "CHI"),
        ("Toronto", "YTO"),
        ("Houston", "HO1"),
    ],
)
def test_multi_airport_city_suggests_metro_code_only(city: str, metro: str) -> None:
    """Picking a multi-airport city must add the all-airports metro code, not
    fragment into individual airports (the CIA-vs-ROM dead-route bug)."""
    codes = _city_suggestion_codes(city)
    assert codes == [metro], f"{city} should resolve to [{metro}], got {codes}"


def test_unverified_metro_codes_not_active() -> None:
    """Codes only IATA-confident but not yet live-verified on Kayak must NOT be
    active, or we'd risk creating a new dead route. Moscow stays as catalog data
    (Pullman Moscow Idaho), not forced to MOW until verified."""
    moscow = _city_suggestion_codes("Moscow")
    # Either no exact 'Moscow' city match, or it is not forced to the MOW metro.
    assert moscow != ["MOW"]


def test_individual_airports_still_searchable() -> None:
    # The specific airports must remain available as airport/airport_code suggestions
    # so a user who really wants FCO can still pick it.
    results = search_location_suggestions("FCO", limit=10)
    assert any("FCO" in item["codes"] for item in results)


def test_single_airport_city_unaffected_by_metro_map() -> None:
    # A city with one airport (not in the metro map) keeps its airport code.
    codes = _codes_for_entry({"label": "Accra", "kind": "city", "codes": ["ACC"]})
    assert codes == ["ACC"]


def test_metro_override_applies_even_to_single_cataloged_airport() -> None:
    # A known metro city resolves to its metro code even if the catalog lists
    # only one airport: the metro code searches ALL airports on Kayak, so this is
    # the desired behavior (e.g. Detroit/Seoul had one cataloged airport).
    codes = _codes_for_entry({"label": "Rome", "kind": "city", "codes": ["FCO"]})
    assert codes == ["ROM"]


def test_non_metro_city_keeps_its_airports() -> None:
    # A city NOT in the metro map is unchanged.
    codes = _codes_for_entry({"label": "Calgary", "kind": "city", "codes": ["YYC"]})
    assert codes == ["YYC"]
