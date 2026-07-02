from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class DailyPriceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_group_id: uuid.UUID
    origin: str
    destination: str
    depart_date: date
    airline: str
    price: float
    currency: str
    provider: str
    deep_link: str | None
    stops: int | None
    stop_label: str | None = None
    duration_minutes: int | None
    # The cheapest offer's itinerary metadata (per-leg actual airports, durations,
    # return date), joined from AllFlightResult so the UI can show the real airport
    # flown (e.g. NRT when TYO metro was searched) -- same data the export uses.
    # Optional/None for rows with no matching detailed result.
    itinerary_data: dict | None = None
    scraped_at: datetime


class PriceTrendPoint(BaseModel):
    depart_date: date
    price: float
    airline: str
