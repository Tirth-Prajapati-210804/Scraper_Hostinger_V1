from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtraLeg:
    """One extra leg of a multi-city itinerary (beyond the first leg).

    destination "" means "back to the segment origin" (resolved per-origin at
    collection time). nights_before = nights between the previous flight and
    this one; the leg departs exactly nights_before days after the previous
    flight (client-validated: 01 Jul + 2 nights -> 03 Jul).
    """

    origin: str
    destination: str
    nights_before: int


@dataclass(frozen=True)
class RouteSegment:
    origin: str
    destinations: list[str]
    trip_type: str
    nights: int | None
    return_origin: str | None = None
    # Multi-city chain beyond leg 1 (1-3 entries = 2-4 total legs). Empty for
    # round trips; for legacy 2-leg multi-city groups it's synthesized from
    # special_sheets + nights so both shapes flow through ONE code path.
    extra_legs: list[ExtraLeg] = field(default_factory=list)


def _clean_code(value: object) -> str:
    return str(value or "").strip().upper()


def _group_extra_legs(group) -> list[ExtraLeg]:
    """The group's extra multi-city legs: new-style first, legacy fallback."""
    raw_legs = getattr(group, "multi_city_legs", None)
    if raw_legs:
        legs: list[ExtraLeg] = []
        for raw in raw_legs:
            raw = raw or {}
            legs.append(
                ExtraLeg(
                    origin=_clean_code(raw.get("origin")),
                    destination=_clean_code(raw.get("destination")),
                    nights_before=max(1, int(raw.get("nights_before") or 1)),
                )
            )
        return legs

    # Legacy 2-leg open-jaw: special_sheets[0].origin -> back to the group
    # origin. nights_before uses the configured nights value directly, matching
    # the round-trip return-date calculation.
    return_origin = None
    if group.special_sheets:
        return_origin = _clean_code(group.special_sheets[0].get("origin")) or None
    if not return_origin:
        return []
    return [
        ExtraLeg(
            origin=return_origin,
            destination="",
            nights_before=max(1, int(group.nights or 1)),
        )
    ]


def iter_group_segments(group) -> list[RouteSegment]:
    segments: list[RouteSegment] = []

    trip_type = str(getattr(group, "trip_type", "") or "round_trip").strip().lower()

    if trip_type == "multi_city":
        extra_legs = _group_extra_legs(group)
        # The segment's "return origin" (used by logs/export labels) is where the
        # final homebound leg departs from.
        return_origin = extra_legs[-1].origin if extra_legs else None

        for origin in group.origins or []:
            segments.append(
                RouteSegment(
                    origin=_clean_code(origin),
                    destinations=[_clean_code(destination) for destination in (group.destinations or [])],
                    trip_type="multi_city",
                    nights=group.nights,
                    return_origin=return_origin,
                    extra_legs=extra_legs,
                )
            )

        return segments

    # ROUND TRIP: collapse a group's multiple origin and destination airports
    # into ONE comma-combined Kayak route so the collector does a SINGLE search
    # per date (e.g. GLA,PIK-MLA or YOW-ORY,CDG). Kayak natively supports comma
    # alternatives in both sides of the URL path (live-confirmed by client).
    # Single-airport sides stay plain codes, so single-origin/single-destination
    # groups behave exactly as before.
    combined_origin = _combined_destination(group.origins)
    combined_destination = _combined_destination(group.destinations)
    if combined_origin:
        segments.append(
            RouteSegment(
                origin=combined_origin,
                destinations=[combined_destination] if combined_destination else [],
                trip_type="round_trip",
                nights=group.nights,
            )
        )

    return segments


def _combined_destination(destinations) -> str:
    """Comma-join a group's destination airports into ONE combined Kayak
    destination token (e.g. ["ORY", "CDG"] -> "ORY,CDG"). De-dupes while
    preserving order and drops blanks. Returns "" if there are no valid
    airports. A single airport returns just that code (no comma), so single-
    destination groups behave exactly as before."""
    seen: list[str] = []
    for destination in destinations or []:
        code = _clean_code(destination)
        if code and code not in seen:
            seen.append(code)
    return ",".join(seen)


def split_alternative_codes(value: object) -> list[str]:
    """Split a comma-joined alternatives field ("ASJ,SES") into clean codes."""
    return [code for code in _clean_code(value).split(",") if code]


def iter_chain_variants(destinations, extra_legs) -> list[tuple[str, list[ExtraLeg]]]:
    """Every concrete multi-city chain a group's alternative airports expand to.

    A leg's origin/destination may hold comma-joined ALTERNATIVES ("ASJ,SES").
    Each variant picks ONE code per field: the cross product of leg-1
    destinations x every extra leg's origin/destination alternatives. The
    collector searches each variant separately per date and saves only the
    cheapest winner. Returns (leg1_destination, resolved_single_code_extra_legs)
    tuples; a config with no alternatives yields exactly one variant, identical
    to the old behavior.
    """
    from itertools import product

    dest_options = [code for value in (destinations or []) for code in split_alternative_codes(value)]
    if not dest_options:
        return []

    per_leg_choices: list[list[ExtraLeg]] = []
    for leg in extra_legs or []:
        origins = split_alternative_codes(leg.origin)
        if not origins:
            return []
        leg_destinations = split_alternative_codes(leg.destination) or [""]
        per_leg_choices.append(
            [
                ExtraLeg(origin=o, destination=d, nights_before=leg.nights_before)
                for o in origins
                for d in leg_destinations
            ]
        )

    variants: list[tuple[str, list[ExtraLeg]]] = []
    for destination in dest_options:
        for combo in product(*per_leg_choices) if per_leg_choices else [()]:
            variants.append((destination, list(combo)))
    return variants


def segment_compares_alternatives(segment) -> bool:
    """True when a multi-city segment expands to MORE THAN ONE chain variant --
    multiple leg-1 destinations and/or comma alternatives on any extra leg -- so
    the collector compares the variants per date and saves one winner. Used by
    the scheduler (compare flag + a date is done once ANY variant saved) and by
    progress (expect 1 row/date). Never True for round trip."""
    if str(getattr(segment, "trip_type", "") or "").strip().lower() != "multi_city":
        return False
    if len(getattr(segment, "destinations", []) or []) > 1:
        return True
    return any(
        "," in str(getattr(leg, "origin", "") or "")
        or "," in str(getattr(leg, "destination", "") or "")
        for leg in (getattr(segment, "extra_legs", None) or [])
    )


def combined_destination_for_group(group) -> str | None:
    """The combined destination key ("ORY,CDG") for a ROUND-TRIP group with more
    than one destination airport, else None. Read paths (prices API, export) use
    this to show ONLY the fresh combined rows for such groups: their legacy
    per-airport rows are kept in the DB (client deletes them when ready) but must
    not surface as duplicate/stale dates next to combined rows. None means "no
    filtering" -- single-destination and multi-city groups read exactly as before."""
    if str(getattr(group, "trip_type", "") or "round_trip") == "multi_city":
        return None
    combined = _combined_destination(getattr(group, "destinations", None))
    return combined if "," in combined else None


def combined_origin_for_group(group) -> str | None:
    """The combined origin key ("GLA,PIK") for a ROUND-TRIP group with more
    than one origin airport, else None. Read/export paths use this to show only
    the fresh combined-origin rows and ignore older per-origin rows."""
    if str(getattr(group, "trip_type", "") or "round_trip") == "multi_city":
        return None
    combined = _combined_destination(getattr(group, "origins", None))
    return combined if "," in combined else None
