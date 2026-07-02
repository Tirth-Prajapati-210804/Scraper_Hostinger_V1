from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db_session
from app.models.all_flight_result import AllFlightResult
from app.models.daily_cheapest import DailyCheapestPrice
from app.models.route_group import RouteGroup
from app.models.user import User
from app.schemas.daily_price import DailyPriceResponse, PriceTrendPoint
from app.services import route_group_service
from app.utils.route_segments import combined_destination_for_group

router = APIRouter(prefix="/prices", tags=["prices"])

_Auth = Annotated[User, Depends(get_current_user)]
_DB = Annotated[AsyncSession, Depends(get_db_session)]
_IATA_QUERY_PATTERN = r"^[A-Za-z0-9]{2,4}$"
# Destination may be a single airport OR a comma-combined multi-airport key
# (e.g. "ORY,CDG") for round-trip groups that do one combined Kayak search. Accept
# 1-8 comma-separated 2-4 char codes; matches the widened destination column.
_DEST_QUERY_PATTERN = r"^[A-Za-z0-9]{2,4}(?:,[A-Za-z0-9]{2,4}){0,7}$"


async def _ensure_accessible_group(
    session: AsyncSession,
    route_group_id: uuid.UUID,
) -> RouteGroup:
    group = await route_group_service.get_by_id(session, route_group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Route group not found",
        )
    return group


@router.get("/", response_model=list[DailyPriceResponse])
async def list_prices(
    session: _DB,
    current_user: _Auth,
    route_group_id: uuid.UUID | None = Query(default=None),
    origin: str | None = Query(default=None, min_length=2, max_length=4, pattern=_IATA_QUERY_PATTERN),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[DailyPriceResponse]:
    group = None
    if route_group_id:
        group = await _ensure_accessible_group(session, route_group_id)

    q = (
        select(DailyCheapestPrice)
        .join(RouteGroup, RouteGroup.id == DailyCheapestPrice.route_group_id)
        .order_by(DailyCheapestPrice.depart_date)
        .offset(offset)
        .limit(limit)
    )
    if route_group_id:
        q = q.where(DailyCheapestPrice.route_group_id == route_group_id)
        # Multi-destination round-trip groups save ONE combined row per date
        # (destination "ORY,CDG"). Their kept legacy per-airport rows must not
        # show as duplicate/stale dates, so read only the combined key.
        combined_key = combined_destination_for_group(group) if group else None
        if combined_key:
            q = q.where(DailyCheapestPrice.destination == combined_key)
    if origin:
        q = q.where(DailyCheapestPrice.origin == origin.upper())
    if date_from:
        q = q.where(DailyCheapestPrice.depart_date >= date_from)
    if date_to:
        q = q.where(DailyCheapestPrice.depart_date <= date_to)

    result = await session.execute(q)
    rows = list(result.scalars().all())

    # Attach itinerary_data (per-leg ACTUAL airports etc.) from the matching detailed
    # offer so the UI can render the real airport flown -- same source as the export.
    # One batched query keyed by (group, origin, destination, depart_date); we keep
    # the offer whose price matches the saved cheapest (the row the export picked).
    itineraries: dict[tuple, dict] = {}
    if rows:
        keys = {(r.route_group_id, r.origin, r.destination, r.depart_date) for r in rows}
        group_ids = {r.route_group_id for r in rows}
        dates = {r.depart_date for r in rows}
        detail_q = select(AllFlightResult).where(
            AllFlightResult.route_group_id.in_(group_ids),
            AllFlightResult.depart_date.in_(dates),
        )
        detail_rows = (await session.execute(detail_q)).scalars().all()
        for d in detail_rows:
            key = (d.route_group_id, d.origin, d.destination, d.depart_date)
            if key not in keys or not isinstance(d.itinerary_data, dict):
                continue
            current = itineraries.get(key)
            # Prefer the cheapest matching offer (mirrors the export's selection).
            if current is None or float(d.price) < float(current.get("_price", 1e18)):
                itineraries[key] = {**d.itinerary_data, "_price": float(d.price)}

    payload: list[DailyPriceResponse] = []
    for r in rows:
        item = DailyPriceResponse.model_validate(r)
        itin = itineraries.get((r.route_group_id, r.origin, r.destination, r.depart_date))
        if itin is not None:
            item.itinerary_data = {k: v for k, v in itin.items() if k != "_price"}
        payload.append(item)
    return payload


@router.get("/trend", response_model=list[PriceTrendPoint])
async def price_trend(
    session: _DB,
    current_user: _Auth,
    origin: str = Query(min_length=2, max_length=4, pattern=_IATA_QUERY_PATTERN),
    destination: str = Query(min_length=2, max_length=64, pattern=_DEST_QUERY_PATTERN),
    route_group_id: uuid.UUID | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> list[PriceTrendPoint]:
    group = None
    if route_group_id:
        group = await _ensure_accessible_group(session, route_group_id)

    # A comma key means different things per trip type. Round trip: rows are saved
    # under the literal combined key ("ORY,CDG" -- one combined Kayak search), so
    # match it exactly. Multi-city: rows are saved under the WINNING airport per
    # date (compare-then-save keeps only the cheapest), so split the key and match
    # any of its airports.
    dest_key = destination.upper()
    if group is not None and group.trip_type == "multi_city" and "," in dest_key:
        destination_filter = DailyCheapestPrice.destination.in_(
            [part for part in dest_key.split(",") if part]
        )
    else:
        destination_filter = DailyCheapestPrice.destination == dest_key

    q = (
        select(DailyCheapestPrice)
        .join(RouteGroup, RouteGroup.id == DailyCheapestPrice.route_group_id)
        .where(
            DailyCheapestPrice.origin == origin.upper(),
            destination_filter,
        )
        .order_by(DailyCheapestPrice.depart_date)
    )
    if route_group_id:
        q = q.where(DailyCheapestPrice.route_group_id == route_group_id)
    if date_from:
        q = q.where(DailyCheapestPrice.depart_date >= date_from)
    if date_to:
        q = q.where(DailyCheapestPrice.depart_date <= date_to)

    result = await session.execute(q)
    return [
        PriceTrendPoint(
            depart_date=p.depart_date,
            price=float(p.price),
            airline=p.airline,
        )
        for p in result.scalars().all()
    ]
