from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteSegment:
    origin: str
    destinations: list[str]
    trip_type: str
    nights: int | None
    return_origin: str | None = None


def iter_group_segments(group) -> list[RouteSegment]:
    segments: list[RouteSegment] = []

    trip_type = str(getattr(group, "trip_type", "") or "round_trip").strip().lower()

    if trip_type == "multi_city":
        return_origin = None
        if group.special_sheets:
            return_origin = str(group.special_sheets[0].get("origin") or "").strip().upper() or None

        for origin in group.origins or []:
            segments.append(
                RouteSegment(
                    origin=str(origin).strip().upper(),
                    destinations=[str(destination).strip().upper() for destination in (group.destinations or [])],
                    trip_type="multi_city",
                    nights=group.nights,
                    return_origin=return_origin,
                )
            )

        return segments

    for origin in group.origins or []:
        segments.append(
            RouteSegment(
                origin=str(origin).strip().upper(),
                destinations=[str(destination).strip().upper() for destination in (group.destinations or [])],
                trip_type="round_trip",
                nights=group.nights,
            )
        )

    return segments
