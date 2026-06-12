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

    for origin in group.origins or []:
        segments.append(
            RouteSegment(
                origin=_clean_code(origin),
                destinations=[_clean_code(destination) for destination in (group.destinations or [])],
                trip_type="round_trip",
                nights=group.nights,
            )
        )

    return segments
