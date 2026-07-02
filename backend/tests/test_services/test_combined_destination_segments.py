"""Combined multi-airport round-trip search: iter_group_segments must collapse a
round-trip group's multiple destination airports into ONE comma-combined
destination (so the collector does a single Kayak search), while multi-city groups
keep per-airport destinations (combined URLs are broken for multi-city)."""
from __future__ import annotations

from types import SimpleNamespace

from app.utils.route_segments import iter_group_segments


def _group(**kw):
    base = dict(
        trip_type="round_trip",
        origins=["YOW"],
        destinations=["ORY", "CDG"],
        nights=9,
        multi_city_legs=None,
        special_sheets=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_round_trip_multi_airport_is_combined_into_one_search():
    segments = iter_group_segments(_group())
    assert len(segments) == 1
    assert segments[0].destinations == ["ORY,CDG"]


def test_round_trip_single_airport_is_unchanged():
    segments = iter_group_segments(_group(destinations=["CDG"]))
    assert segments[0].destinations == ["CDG"]


def test_round_trip_combined_dedupes_and_drops_blanks_preserving_order():
    segments = iter_group_segments(_group(destinations=["ORY", "CDG", "ORY", "BVA", ""]))
    assert segments[0].destinations == ["ORY,CDG,BVA"]


def test_round_trip_multi_origin_each_origin_gets_combined_destination():
    segments = iter_group_segments(_group(origins=["YOW", "YYC"]))
    assert [(s.origin, s.destinations) for s in segments] == [
        ("YOW", ["ORY,CDG"]),
        ("YYC", ["ORY,CDG"]),
    ]


def test_multi_city_keeps_per_airport_destinations_not_combined():
    # Combined comma URLs are broken for multi-city, so it must NOT combine.
    segments = iter_group_segments(
        _group(trip_type="multi_city", multi_city_legs=[{"origin": "CDG", "destination": "", "nights_before": 3}])
    )
    assert segments[0].destinations == ["ORY", "CDG"]


def test_combined_destination_for_group_multi_dest_round_trip():
    from app.utils.route_segments import combined_destination_for_group

    assert combined_destination_for_group(_group()) == "ORY,CDG"


def test_combined_destination_for_group_none_for_single_and_multi_city():
    # None = "no read filtering": single-destination round trips and every
    # multi-city group must read exactly as before (their rows are per-airport).
    from app.utils.route_segments import combined_destination_for_group

    assert combined_destination_for_group(_group(destinations=["CDG"])) is None
    assert (
        combined_destination_for_group(
            _group(
                trip_type="multi_city",
                multi_city_legs=[{"origin": "CDG", "destination": "", "nights_before": 3}],
            )
        )
        is None
    )
