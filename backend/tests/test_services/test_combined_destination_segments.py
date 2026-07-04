"""Combined multi-airport round-trip search: iter_group_segments must collapse a
round-trip group's multiple origin/destination airports into ONE comma-combined
Kayak route (so the collector does a single search), while multi-city groups keep
per-airport variants (combined URLs are broken for multi-city)."""
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


def test_round_trip_multi_origin_is_combined_into_one_search():
    segments = iter_group_segments(_group(origins=["YOW", "YYC"]))
    assert [(s.origin, s.destinations) for s in segments] == [
        ("YOW,YYC", ["ORY,CDG"]),
    ]


def test_round_trip_multi_origin_single_destination_is_one_combined_route():
    segments = iter_group_segments(_group(origins=["GLA", "PIK"], destinations=["MLA"]))
    assert len(segments) == 1
    assert segments[0].origin == "GLA,PIK"
    assert segments[0].destinations == ["MLA"]


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


def test_combined_origin_for_group_multi_origin_round_trip():
    from app.utils.route_segments import combined_origin_for_group

    assert (
        combined_origin_for_group(_group(origins=["GLA", "PIK"], destinations=["MLA"]))
        == "GLA,PIK"
    )


def test_iter_chain_variants_expands_leg_alternatives():
    # Leg fields may hold comma-joined ALTERNATIVES; every combination becomes
    # its own searchable chain (dest x leg-origin x leg-destination).
    from app.utils.route_segments import ExtraLeg, iter_chain_variants

    variants = iter_chain_variants(
        ["SEL"],
        [ExtraLeg(origin="ASJ,SES", destination="", nights_before=5)],
    )
    assert [(d, [(l.origin, l.destination) for l in legs]) for d, legs in variants] == [
        ("SEL", [("ASJ", "")]),
        ("SEL", [("SES", "")]),
    ]

    # Cross product with multiple leg-1 destinations.
    variants = iter_chain_variants(
        ["ICN", "GMP"],
        [ExtraLeg(origin="ASJ,SES", destination="", nights_before=5)],
    )
    assert len(variants) == 4

    # No alternatives -> exactly one variant (old behavior).
    variants = iter_chain_variants(
        ["BER"],
        [ExtraLeg(origin="BUD", destination="", nights_before=9)],
    )
    assert len(variants) == 1


def test_segment_compares_alternatives_predicate():
    from app.utils.route_segments import ExtraLeg, segment_compares_alternatives

    def seg(trip_type="multi_city", destinations=("BER",), legs=()):
        return SimpleNamespace(
            trip_type=trip_type,
            destinations=list(destinations),
            extra_legs=list(legs),
        )

    # Comma alternatives on an extra leg trigger compare even with ONE leg-1 dest.
    assert segment_compares_alternatives(
        seg(legs=[ExtraLeg(origin="ASJ,SES", destination="", nights_before=3)])
    )
    # Multiple leg-1 destinations trigger it too.
    assert segment_compares_alternatives(seg(destinations=["BER", "BUD"]))
    # Single-variant multi-city and every round trip do not.
    assert not segment_compares_alternatives(
        seg(legs=[ExtraLeg(origin="BUD", destination="", nights_before=3)])
    )
    assert not segment_compares_alternatives(seg(trip_type="round_trip", destinations=["A", "B"]))
