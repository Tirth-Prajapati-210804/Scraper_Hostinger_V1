from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.models.all_flight_result import AllFlightResult
from app.models.scrape_log import ScrapeLog
from app.providers.base import (
    FlightProvider,
    ProviderAuthError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderSearchDiagnostics,
    ProviderSearchOutcome,
    ProviderResult,
)
from app.utils.airline_codes import normalize_airline

log = get_logger(__name__)
_GENERIC_MULTI_AIRLINE_LABELS = {
    "multiple airlines",
    "multiple airline",
    "mixed airlines",
    "various airlines",
}


def _derive_return_date(depart_date: date, nights: int) -> date:
    """
    Return after the configured number of nights at destination.
    Depart Apr 1 + 12 nights = Apr 13.
    """
    return depart_date + timedelta(days=max(1, nights))


def _build_multi_city_legs(
    *,
    origin: str,
    destination: str,
    depart_date: date,
    extra_legs: list | None,
    nights: int | None,
    return_origin: str | None,
) -> list[dict[str, object]]:
    """The full 2-4 leg chain for one date.

    Leg 1 is always origin->destination on depart_date. Each extra leg departs
    nights_before days after the previous flight (client-validated example:
    LON-KEF 01 Jul + 2 nights -> KEF-YYZ 03 Jul; +2 nights Toronto +3 nights
    New York = 5 -> NYC-LON 08 Jul). An empty extra-leg destination means
    "back to the group origin". Without extra_legs this uses the same nights
    offset as round trips.
    """
    legs: list[dict[str, object]] = [
        {"departure_id": origin, "arrival_id": destination, "outbound_date": depart_date},
    ]
    if extra_legs:
        current = depart_date
        for extra in extra_legs:
            current = current + timedelta(days=max(1, int(extra.nights_before)))
            legs.append(
                {
                    "departure_id": extra.origin,
                    "arrival_id": extra.destination or origin,
                    "outbound_date": current,
                }
            )
        return legs

    legs.append(
        {
            "departure_id": return_origin,
            "arrival_id": origin,
            "outbound_date": _derive_return_date(depart_date, nights or 1),
        }
    )
    return legs


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, ProviderQuotaExhaustedError):
        return "quota_exhausted"
    if isinstance(exc, ProviderAuthError):
        return "auth_error"
    if isinstance(exc, ProviderRateLimitedError):
        return "rate_limited"
    message = str(exc).lower()
    if "invalid json" in message or "parse" in message:
        return "parse_error"
    return "provider_error"


def _friendly_error(exc: BaseException, status: str) -> str:
    """Short, plain-English error for the UI/logs.

    Provider responses (esp. ScrapingBee's 500 body) are long and leak internal
    advice like "try premium_proxy ... 75 credits". We never want that in the
    dashboard, so map the raw text to a brief, user-meaningful reason. Falls back
    to a trimmed redacted message for anything unrecognised.
    """
    raw = str(exc)
    lowered = raw.lower()
    if status == "quota_exhausted":
        return "ScrapingBee credits exhausted - top up to resume collection."
    if status == "auth_error":
        return "ScrapingBee API key rejected - check the configured key."
    if status == "rate_limited":
        return "ScrapingBee rate limit hit - will retry shortly."
    if status == "parse_error":
        return "Could not read the rendered page - will retry."
    # provider_error: the common ScrapingBee 500 / timeout family.
    if "timed out" in lowered or "timeout" in lowered:
        return "Kayak render timed out - will retry."
    if "error with your request" in lowered or "you will not be charged" in lowered:
        return "Kayak page did not render in time - will retry."
    if "did not expose extractable" in lowered or "no result" in lowered:
        return "Kayak returned no readable fares for this date."
    # Unknown: keep it short and scrubbed instead of dumping the raw body.
    cleaned = redact_text(raw).strip()
    return (cleaned[:140] + "...") if len(cleaned) > 140 else (cleaned or "Scrape failed - will retry.")


@dataclass
class CollectionResult:
    origin: str
    destination: str
    depart_date: date
    cheapest: ProviderResult | None
    return_date: date | None = None
    stop_label: str | None = None
    provider_results: dict[str, list[ProviderResult]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    provider_diagnostics: dict[str, ProviderSearchDiagnostics] = field(default_factory=dict)


class PriceCollector:
    """
    Goal B v2:
    - smart date priority
    - dead route cooldown
    - lower quota waste
    - faster useful coverage
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        providers: list[FlightProvider],
        on_provider_success: Callable[[str], None] | None = None,
        on_provider_failure: Callable[[str, BaseException], None] | None = None,
        on_item_started: Callable[[str, str, date, bool], None] | None = None,
        on_item_progress: Callable[[str, str, str, date, bool], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.on_provider_success = on_provider_success
        self.on_provider_failure = on_provider_failure
        self.on_item_started = on_item_started
        self.on_item_progress = on_item_progress

        self._route_failures: dict[str, int] = {}
        self._route_cooldown: dict[str, int] = {}

    # --------------------------------------------------
    # DATE PRIORITY
    # --------------------------------------------------

    def _score_date(self, d: date) -> tuple[int, int]:
        today = date.today()
        days_out = (d - today).days

        weekend_bonus = 0 if d.weekday() in (4, 5, 6) else 20
        near_bonus = min(max(days_out, 0), 365)

        return (near_bonus + weekend_bonus, days_out)

    def _prioritize_dates(self, dates: list[date]) -> list[date]:
        return sorted(set(dates), key=self._score_date)

    # --------------------------------------------------
    # ROUTE HEALTH
    # --------------------------------------------------

    def _route_key(
        self,
        origin: str,
        destination: str,
    ) -> str:
        return f"{origin}:{destination}"

    def _is_route_cooled(self, key: str) -> bool:
        remaining = self._route_cooldown.get(key, 0)

        if remaining <= 0:
            return False

        self._route_cooldown[key] = remaining - 1
        return True

    def _mark_route_success(self, key: str):
        self._route_failures[key] = 0
        self._route_cooldown.pop(key, None)

    def _mark_route_failure(self, key: str):
        fails = self._route_failures.get(key, 0) + 1
        self._route_failures[key] = fails

        # cooldown after repeated waste
        if fails >= 3:
            self._route_cooldown[key] = min(fails * 2, 12)

    def _provider_search_kwargs(
        self,
        provider: FlightProvider,
        *,
        market: str | None,
        same_airline_only: bool = True,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> dict[str, object]:
        kwargs: dict[str, object] = {}
        if getattr(provider, "name", "") == "scrapingbee":
            if market:
                kwargs["market"] = market
            # Per-group toggle: ON = only same-carrier itineraries qualify
            # (airlines=-MULT,flylocal in the URL + the Python carrier filter);
            # OFF = cheapest itinerary regardless of carrier mix.
            kwargs["same_airline_only"] = bool(same_airline_only)
            # Carried into the Kayak URL (legdur=/layoverdur=) so Kayak filters
            # server-side before render -- fewer cards, faster extract.
            if max_leg_duration_minutes:
                kwargs["max_leg_duration_minutes"] = max_leg_duration_minutes
            if max_layover_minutes:
                kwargs["max_layover_minutes"] = max_layover_minutes
        return kwargs

    def _normalize_stop_mode(self, max_stops: int | None) -> int | None:
        if max_stops is None:
            return None
        return max_stops

    def _allowed_leg_stop_limit(self, stop_count: int | None) -> int | None:
        if stop_count is None:
            return None
        if stop_count <= 0:
            return 0
        if stop_count == 1:
            return 1
        return 2

    def _stop_label_for_count(self, stops: int) -> str:
        if stops <= 0:
            return "Direct"
        if stops == 1:
            return "1 Stop"
        return f"{stops} Stops"

    def _tokenize_airline_value(self, value: object) -> list[str]:
        if not isinstance(value, str):
            return []
        cleaned = value.strip()
        if not cleaned:
            return []
        if cleaned.casefold() in _GENERIC_MULTI_AIRLINE_LABELS:
            return ["__multiple__"]
        parts = [
            part.strip()
            for part in re.split(r"\s*[/,;|]\s*", cleaned)
            if part and part.strip()
        ]
        if not parts:
            parts = [cleaned]
        return parts

    def _result_airline_names(self, result: ProviderResult) -> list[str]:
        raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
        raw_values: list[object] = []

        airline_names = raw_data.get("airline_names")
        if isinstance(airline_names, list):
            raw_values.extend(airline_names)

        legs = raw_data.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if isinstance(leg, dict):
                    raw_values.append(leg.get("airline"))

        raw_values.append(raw_data.get("outbound_airline"))
        raw_values.append(raw_data.get("return_airline"))

        if isinstance(result.airline, str):
            raw_values.append(result.airline)

        names: list[str] = []
        for raw_value in raw_values:
            for token in self._tokenize_airline_value(raw_value):
                if token == "__multiple__":
                    names.append(token)
                    continue
                normalized = normalize_airline(token).strip()
                if normalized and normalized != "-":
                    names.append(normalized)
        return names

    def _same_airline_results_only(self, results: list[ProviderResult]) -> list[ProviderResult]:
        filtered: list[ProviderResult] = []
        for result in results:
            airline_names = self._result_airline_names(result)
            if "__multiple__" in airline_names:
                continue
            airline_keys = {name.casefold() for name in airline_names}
            if len(airline_keys) != 1:
                continue
            result.airline = airline_names[0]
            filtered.append(result)
        return filtered

    def _result_leg_stops(self, result: ProviderResult, trip_type: str) -> list[int]:
        raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
        leg_stops = raw_data.get("leg_stops")
        if isinstance(leg_stops, list):
            normalized = [
                int(value)
                for value in leg_stops
                if isinstance(value, (int, float))
            ]
            if normalized:
                return normalized
        return [result.stops]

    def _result_stop_label(self, result: ProviderResult, trip_type: str) -> str:
        raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
        raw_label = raw_data.get("stop_result_label")
        if isinstance(raw_label, str) and raw_label.strip():
            return raw_label.strip()

        labels = [
            self._stop_label_for_count(stops)
            for stops in self._result_leg_stops(result, trip_type)
        ]
        return " / ".join(labels) if labels else self._stop_label_for_count(result.stops)

    def _result_leg_durations(self, result: ProviderResult, trip_type: str) -> list[int]:
        raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
        raw_durations = raw_data.get("leg_durations")
        if isinstance(raw_durations, list):
            durations = [
                int(value)
                for value in raw_durations
                if isinstance(value, (int, float)) and int(value) > 0
            ]
            if durations:
                return durations

        raw_legs = raw_data.get("legs")
        if isinstance(raw_legs, list):
            durations = []
            for leg in raw_legs:
                if not isinstance(leg, dict):
                    continue
                duration = leg.get("duration_minutes")
                if isinstance(duration, (int, float)) and int(duration) > 0:
                    durations.append(int(duration))
            if durations:
                return durations

        if trip_type == "round_trip" and result.duration_minutes and result.duration_minutes > 0:
            return [int(result.duration_minutes)]

        return []

    def _duration_results_only(
        self,
        results: list[ProviderResult],
        max_leg_duration_minutes: int | None,
        trip_type: str,
    ) -> list[ProviderResult]:
        if not max_leg_duration_minutes:
            return results

        filtered: list[ProviderResult] = []
        for result in results:
            leg_durations = self._result_leg_durations(result, trip_type)
            if leg_durations and all(duration <= max_leg_duration_minutes for duration in leg_durations):
                filtered.append(result)
        return filtered

    def _exact_stop_results_only(
        self,
        results: list[ProviderResult],
        stop_count: int | None,
        trip_type: str,
    ) -> list[ProviderResult]:
        limit = self._allowed_leg_stop_limit(stop_count)
        if limit is None:
            return results

        filtered: list[ProviderResult] = []
        for result in results:
            leg_stops = self._result_leg_stops(result, trip_type)
            if leg_stops and all(stops <= limit for stops in leg_stops):
                filtered.append(result)
        return filtered

    def _result_sort_key(self, result: ProviderResult) -> tuple[float, int, int]:
        duration_rank = result.duration_minutes if result.duration_minutes and result.duration_minutes > 0 else 10**9
        stops_rank = result.stops if result.stops >= 0 else 10**9
        return (result.price, duration_rank, stops_rank)

    def _detected_currency_label(self, results: list[ProviderResult]) -> str | None:
        currencies = sorted(
            {
                str(result.currency).strip().upper()
                for result in results
                if str(result.currency).strip()
            }
        )
        if not currencies:
            return None
        if len(currencies) == 1:
            return currencies[0]
        return ",".join(currencies)

    async def _search_with_diagnostics(
        self,
        provider: FlightProvider,
        *,
        trip_type: str,
        origin: str,
        destination: str,
        depart_date: date,
        currency: str,
        requested_stop_mode: int | None,
        market: str | None,
        nights: int | None,
        return_origin: str | None,
        return_date: date | None,
        same_airline_only: bool,
        extra_legs: list | None = None,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        if trip_type == "multi_city":
            method = getattr(provider, "search_multi_city_diagnostic", None)
            legs = _build_multi_city_legs(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                extra_legs=extra_legs,
                nights=nights,
                return_origin=return_origin,
            )
            if callable(method):
                return await method(
                    legs=legs,
                    currency=currency,
                    max_stops=requested_stop_mode,
                    **self._provider_search_kwargs(
                        provider,
                        market=market,
                        same_airline_only=same_airline_only,
                        max_leg_duration_minutes=max_leg_duration_minutes,
                        max_layover_minutes=max_layover_minutes,
                    ),
                )
            results = await provider.search_multi_city(
                legs=legs,
                currency=currency,
                max_stops=requested_stop_mode,
                **self._provider_search_kwargs(
                    provider,
                    market=market,
                    same_airline_only=same_airline_only,
                ),
            )
        else:
            method = getattr(provider, "search_round_trip_diagnostic", None)
            if callable(method):
                return await method(
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    return_date=return_date,
                    currency=currency,
                    max_stops=requested_stop_mode,
                    **self._provider_search_kwargs(
                        provider,
                        market=market,
                        max_leg_duration_minutes=max_leg_duration_minutes,
                        max_layover_minutes=max_layover_minutes,
                    ),
                )
            results = await provider.search_round_trip(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                return_date=return_date,
                currency=currency,
                max_stops=requested_stop_mode,
                **self._provider_search_kwargs(
                    provider,
                    market=market,
                ),
            )

        diagnostics = ProviderSearchDiagnostics(
            raw_offers_found=len(results),
            eligible_offers_found=len(results),
            requested_market=market,
            requested_currency=currency,
            detected_currencies=(
                [currency_label]
                if (currency_label := self._detected_currency_label(results)) is not None
                else []
            ),
        )
        return ProviderSearchOutcome(results=results, diagnostics=diagnostics)

    # --------------------------------------------------
    # SINGLE SEARCH
    # --------------------------------------------------

    async def collect_single_date(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        route_group_id: UUID | None,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        trip_type: str = "round_trip",
        nights: int | None = None,
        return_origin: str | None = None,
        same_airline_only: bool = True,
        extra_legs: list | None = None,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> CollectionResult:

        all_results: list[ProviderResult] = []
        provider_results: dict[str, list[ProviderResult]] = {}
        provider_diagnostics: dict[str, ProviderSearchDiagnostics] = {}
        errors: dict[str, str] = {}
        return_date: date | None = None
        requested_stop_mode = self._normalize_stop_mode(max_stops)
        effective_trip_type = "multi_city" if trip_type == "multi_city" else "round_trip"
        # Honor the per-group toggle (it used to be force-overridden to True here).
        same_airline_only = bool(same_airline_only)

        async with self.session_factory() as session:
            for provider in self.providers:
                start = time.monotonic()

                try:
                    if effective_trip_type == "multi_city":
                        stay_nights = nights or 1
                        if not return_origin and not extra_legs:
                            raise RuntimeError("multi_city collection requires a return origin.")

                        if extra_legs:
                            # Final homebound leg date = depart + each leg's
                            # nights_before, chained (client's own arithmetic).
                            return_date = depart_date
                            for extra in extra_legs:
                                return_date = return_date + timedelta(
                                    days=max(1, int(extra.nights_before))
                                )
                        else:
                            return_date = _derive_return_date(depart_date, stay_nights)
                        outcome = await self._search_with_diagnostics(
                            provider,
                            trip_type=effective_trip_type,
                            origin=origin,
                            destination=destination,
                            depart_date=depart_date,
                            currency=currency,
                            requested_stop_mode=requested_stop_mode,
                            market=market,
                            nights=stay_nights,
                            return_origin=return_origin,
                            return_date=return_date,
                            same_airline_only=same_airline_only,
                            extra_legs=extra_legs,
                            max_leg_duration_minutes=max_leg_duration_minutes,
                            max_layover_minutes=max_layover_minutes,
                        )
                    else:
                        stay_nights = nights or 3
                        return_date = _derive_return_date(depart_date, stay_nights)
                        outcome = await self._search_with_diagnostics(
                            provider,
                            trip_type=effective_trip_type,
                            origin=origin,
                            destination=destination,
                            depart_date=depart_date,
                            currency=currency,
                            requested_stop_mode=requested_stop_mode,
                            market=market,
                            nights=stay_nights,
                            return_origin=return_origin,
                            return_date=return_date,
                            same_airline_only=same_airline_only,
                            max_leg_duration_minutes=max_leg_duration_minutes,
                            max_layover_minutes=max_layover_minutes,
                        )

                    raw_results = list(outcome.results)
                    diagnostics = outcome.diagnostics
                    provider_raw_offers_found = diagnostics.raw_offers_found
                    after_stop = self._exact_stop_results_only(raw_results, requested_stop_mode, effective_trip_type)
                    filtered_by_stop_count = max(0, len(raw_results) - len(after_stop))
                    after_duration = self._duration_results_only(
                        after_stop,
                        max_leg_duration_minutes,
                        effective_trip_type,
                    )
                    filtered_by_duration = max(0, len(after_stop) - len(after_duration))
                    final_results = after_duration
                    filtered_by_same_airline = 0

                    if same_airline_only:
                        same_airline_results = self._same_airline_results_only(final_results)
                        filtered_by_same_airline = max(0, len(final_results) - len(same_airline_results))
                        final_results = same_airline_results

                    elapsed_ms = int((time.monotonic() - start) * 1000)

                    detected_currency = self._detected_currency_label(raw_results or final_results)
                    result_reason = diagnostics.result_reason
                    if final_results:
                        result_reason = "success"
                    elif raw_results:
                        requested_currency = currency.strip().upper()
                        detected_currencies = {
                            str(result.currency).strip().upper()
                            for result in raw_results
                            if str(result.currency).strip()
                        }
                        if (
                            requested_currency
                            and detected_currencies
                            and requested_currency not in detected_currencies
                        ):
                            result_reason = "market_mismatch"
                        else:
                            result_reason = "filtered_out"
                    else:
                        result_reason = result_reason or (
                            "extract_failed"
                            if diagnostics.visible_results_found or diagnostics.summary_price_found
                            else "page_empty"
                        )

                    diagnostics.result_reason = result_reason
                    diagnostics.raw_offers_found = max(provider_raw_offers_found, len(raw_results))
                    diagnostics.eligible_offers_found = len(final_results)
                    diagnostics.requested_market = market
                    diagnostics.requested_currency = currency
                    diagnostics.detected_currencies = [detected_currency] if detected_currency else []
                    provider_diagnostics[provider.name] = diagnostics

                    provider_results[provider.name] = final_results
                    all_results.extend(final_results)
                    if self.on_provider_success:
                        self.on_provider_success(provider.name)

                    session.add(
                        ScrapeLog(
                            route_group_id=route_group_id,
                            origin=origin,
                            destination=destination,
                            depart_date=depart_date,
                            provider=provider.name,
                            status="success" if final_results else "no_results",
                            offers_found=len(final_results),
                            result_reason=result_reason,
                            raw_offers_found=diagnostics.raw_offers_found,
                            eligible_offers_found=len(final_results),
                            filtered_by_stop_count=filtered_by_stop_count,
                            filtered_by_same_airline=filtered_by_same_airline,
                            filtered_by_duration=filtered_by_duration,
                            requested_market=market,
                            requested_currency=currency,
                            detected_currency=detected_currency,
                            cheapest_price=(
                                min(final_results, key=self._result_sort_key).price
                                if final_results
                                else None
                            ),
                            duration_ms=elapsed_ms,
                        )
                    )

                except Exception as exc:
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    status = _classify_exception(exc)
                    # Store a SHORT, plain-English reason -- never the raw provider
                    # body (ScrapingBee's 500 leaks long internal advice). Friendly
                    # text is capped well under the column limit.
                    safe_error = _friendly_error(exc, status)

                    errors[provider.name] = safe_error
                    if self.on_provider_failure:
                        self.on_provider_failure(provider.name, exc)

                    session.add(
                        ScrapeLog(
                            route_group_id=route_group_id,
                            origin=origin,
                            destination=destination,
                            depart_date=depart_date,
                            provider=provider.name,
                            status=status,
                            offers_found=0,
                            error_message=safe_error[:300],
                            duration_ms=elapsed_ms,
                        )
                    )

            cheapest = (
                min(all_results, key=self._result_sort_key)
                if all_results else None
            )

            if cheapest:
                await self._upsert_cheapest(
                    session,
                    route_group_id,
                    origin,
                    destination,
                    depart_date,
                    cheapest,
                )

                await self._save_all_results(
                    session,
                    route_group_id,
                    origin,
                    destination,
                    depart_date,
                    all_results,
                )
            await session.commit()

        return CollectionResult(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            cheapest=cheapest,
            return_date=return_date if effective_trip_type == "multi_city" else None,
            stop_label=(
                self._result_stop_label(cheapest, effective_trip_type)
                if cheapest
                else None
            ),
            provider_results=provider_results,
            errors=errors,
            provider_diagnostics=provider_diagnostics,
        )

    # --------------------------------------------------
    # MAIN BATCH
    # --------------------------------------------------

    async def collect_route_batch(
        self,
        origin: str,
        destinations: list[str],
        dates: list[date],
        route_group_id: UUID,
        batch_size: int = 4,
        delay_seconds: float = 1.2,
        stop_check: Callable[[], bool] | None = None,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        trip_type: str = "round_trip",
        nights: int | None = None,
        return_origin: str | None = None,
        same_airline_only: bool = True,
        extra_legs: list | None = None,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
        is_retry: bool = False,
    ) -> dict[str, int]:

        stats = {
            "success": 0,
            "errors": 0,
            "skipped": 0,
        }

        prioritized_dates = self._prioritize_dates(dates)
        semaphore = asyncio.Semaphore(batch_size)

        async def await_with_stop(coro):
            task = asyncio.create_task(coro)
            try:
                while not task.done():
                    if stop_check and stop_check():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        return None, True
                    await asyncio.sleep(0.25)
                return await task, False
            finally:
                if not task.done():
                    task.cancel()

        async def run_one(dest: str, depart_date: date):
            route_key = self._route_key(origin, dest)

            if self._is_route_cooled(route_key):
                if self.on_item_progress:
                    self.on_item_progress("skipped", origin, dest, depart_date, is_retry)
                return "skipped"

            if stop_check and stop_check():
                return "stopped"

            async with semaphore:
                if stop_check and stop_check():
                    return "stopped"

                if self.on_item_started:
                    self.on_item_started(origin, dest, depart_date, is_retry)

                try:
                    result, was_stopped = await await_with_stop(
                        self.collect_single_date(
                            origin=origin,
                            destination=dest,
                            depart_date=depart_date,
                            route_group_id=route_group_id,
                            market=market,
                            currency=currency,
                            max_stops=max_stops,
                            trip_type=trip_type,
                            nights=nights,
                            return_origin=return_origin,
                            same_airline_only=same_airline_only,
                            extra_legs=extra_legs,
                            max_leg_duration_minutes=max_leg_duration_minutes,
                            max_layover_minutes=max_layover_minutes,
                        )
                    )
                    if was_stopped or result is None:
                        return "stopped"

                    if result.cheapest:
                        self._mark_route_success(route_key)
                        if self.on_item_progress:
                            self.on_item_progress("success", origin, dest, depart_date, is_retry)
                        return "success"

                    if self.on_item_progress:
                        self.on_item_progress("skipped", origin, dest, depart_date, is_retry)
                    return "skipped"

                except Exception as exc:
                    self._mark_route_failure(route_key)

                    log.warning(
                        "collect_single_failed",
                        origin=origin,
                        destination=dest,
                        date=str(depart_date),
                        error=redact_text(str(exc)),
                    )

                    if self.on_item_progress:
                        self.on_item_progress("error", origin, dest, depart_date, is_retry)
                    return "error"

        tasks = []

        for depart_date in prioritized_dates:
            for dest in destinations:
                tasks.append(run_one(dest, depart_date))

        for i in range(0, len(tasks), batch_size):
            if stop_check and stop_check():
                break

            chunk = tasks[i:i + batch_size]

            results = await asyncio.gather(
                *chunk,
                return_exceptions=True,
            )

            for r in results:
                if r == "success":
                    stats["success"] += 1
                elif r in {"skipped", "stopped"}:
                    stats["skipped"] += 1
                else:
                    stats["errors"] += 1

            if i + batch_size < len(tasks):
                slept = 0.0
                while slept < delay_seconds:
                    if stop_check and stop_check():
                        break
                    interval = min(0.25, delay_seconds - slept)
                    await asyncio.sleep(interval)
                    slept += interval

        return stats

    # --------------------------------------------------
    # DB HELPERS
    # --------------------------------------------------

    async def _upsert_cheapest(
        self,
        session: AsyncSession,
        route_group_id: UUID,
        origin: str,
        destination: str,
        depart_date: date,
        result: ProviderResult,
    ) -> None:

        await session.execute(
            text("""
                INSERT INTO daily_cheapest_prices
                (
                    id,
                    route_group_id,
                    origin,
                    destination,
                    depart_date,
                    airline,
                    price,
                    currency,
                    provider,
                    deep_link,
                    stops,
                    stop_label,
                    duration_minutes,
                    scraped_at
                )
                VALUES
                (
                    gen_random_uuid(),
                    :route_group_id,
                    :origin,
                    :destination,
                    :depart_date,
                    :airline,
                    :price,
                    :currency,
                    :provider,
                    :deep_link,
                    :stops,
                    :stop_label,
                    :duration_minutes,
                    now()
                )
                ON CONFLICT (route_group_id, origin, destination, depart_date)
                DO UPDATE SET
                    airline = EXCLUDED.airline,
                    price = EXCLUDED.price,
                    currency = EXCLUDED.currency,
                    provider = EXCLUDED.provider,
                    deep_link = EXCLUDED.deep_link,
                    stops = EXCLUDED.stops,
                    stop_label = EXCLUDED.stop_label,
                    duration_minutes = EXCLUDED.duration_minutes,
                    scraped_at = now()
            """),
            {
                "route_group_id": str(route_group_id),
                "origin": origin,
                "destination": destination,
                "depart_date": depart_date,
                "airline": normalize_airline(result.airline),
                "price": result.price,
                "currency": result.currency,
                "provider": result.provider or "unknown",
                "deep_link": result.deep_link[:2048] if result.deep_link else None,
                "stops": result.stops,
                "stop_label": self._result_stop_label(
                    result,
                    str(result.raw_data.get("trip_type"))
                    if isinstance(result.raw_data, dict) and result.raw_data.get("trip_type")
                    else "round_trip",
                ),
                "duration_minutes": result.duration_minutes,
            },
        )

    async def _save_all_results(
        self,
        session: AsyncSession,
        route_group_id: UUID,
        origin: str,
        destination: str,
        depart_date: date,
        results: list[ProviderResult],
    ) -> None:

        await session.execute(
            text("""
                DELETE FROM all_flight_results
                WHERE route_group_id = :rg_id
                  AND origin = :origin
                  AND destination = :destination
                  AND depart_date = :depart_date
            """),
            {
                "rg_id": str(route_group_id),
                "origin": origin,
                "destination": destination,
                "depart_date": depart_date,
            },
        )

        for result in sorted(results, key=self._result_sort_key):
            session.add(
                AllFlightResult(
                    route_group_id=route_group_id,
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    airline=normalize_airline(result.airline),
                    price=result.price,
                    currency=result.currency,
                    provider=result.provider or "unknown",
                    deep_link=result.deep_link[:2048] if result.deep_link else None,
                    stops=result.stops,
                    stop_label=self._result_stop_label(
                        result,
                        str(result.raw_data.get("trip_type"))
                        if isinstance(result.raw_data, dict) and result.raw_data.get("trip_type")
                        else "round_trip",
                    ),
                    duration_minutes=result.duration_minutes,
                    itinerary_data=result.raw_data if isinstance(result.raw_data, dict) else None,
                )
            )
