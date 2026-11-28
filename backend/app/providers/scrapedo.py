from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import date
from time import monotonic
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.logging import get_logger
from app.providers.base import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderResult,
    ProviderSearchDiagnostics,
    ProviderSearchOutcome,
)
from app.utils.airline_codes import normalize_airline

log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.scrape.do"
_KAYAK_DEFAULT_HOST = "www.kayak.com"
_MARKET_RE = re.compile(r"^[a-z]{2}$")
_POLL_SEGMENT_CARRIER_RE = re.compile(r"^\d{13}([A-Z0-9]{2})")
_NON_FLIGHT_AIRLINE_CODES = {"9B", "2A"}
_NON_FLIGHT_RE = re.compile(
    r"\b(?:rail|train|coach|motorcoach|bus|ferry)\b|accesrail|eurostar|national\s+express|flixbus|amtrak",
    re.IGNORECASE,
)

# Maps both 2-letter UI markets (including all 23 markets in RouteGroupForm.tsx)
# and internal ISO country aliases to the regional KAYAK domain.
_KAYAK_HOST_BY_COUNTRY = {
    "ae": "www.kayak.ae",
    "au": "www.kayak.com.au",
    "br": "www.kayak.com.br",
    "ca": "www.ca.kayak.com",
    "ch": "www.kayak.ch",
    "de": "www.kayak.de",
    "dk": "www.kayak.dk",
    "es": "www.kayak.es",
    "fi": "www.kayak.fi",
    "fr": "www.kayak.fr",
    "gb": "www.kayak.co.uk",
    "ie": "www.kayak.ie",
    "in": "www.kayak.co.in",
    "it": "www.kayak.it",
    "jp": "www.kayak.co.jp",
    "mx": "www.kayak.com.mx",
    "nl": "www.kayak.nl",
    "no": "www.kayak.no",
    "nz": "www.kayak.co.nz",
    "se": "www.kayak.se",
    "sg": "www.kayak.sg",
    "uk": "www.kayak.co.uk",
    "us": "www.kayak.com",
    "za": "www.kayak.co.za",
}

_PROXY_GEO_ALIASES = {
    "uk": "gb",
}

_COUNTRY_CODE_BY_CURRENCY = {
    "AED": "ae",
    "AUD": "au",
    "BRL": "br",
    "CAD": "ca",
    "CHF": "ch",
    "DKK": "dk",
    "EUR": "ie",
    "GBP": "uk",
    "INR": "in",
    "JPY": "jp",
    "MXN": "mx",
    "NOK": "no",
    "NZD": "nz",
    "SEK": "se",
    "SGD": "sg",
    "USD": "us",
    "ZAR": "za",
}

_CURRENCY_BY_COUNTRY = {
    "ae": "AED",
    "au": "AUD",
    "br": "BRL",
    "ca": "CAD",
    "ch": "CHF",
    "de": "EUR",
    "dk": "DKK",
    "es": "EUR",
    "fi": "EUR",
    "fr": "EUR",
    "gb": "GBP",
    "ie": "EUR",
    "in": "INR",
    "it": "EUR",
    "jp": "JPY",
    "mx": "MXN",
    "nl": "EUR",
    "no": "NOK",
    "nz": "NZD",
    "se": "SEK",
    "sg": "SGD",
    "uk": "GBP",
    "us": "USD",
    "za": "ZAR",
}

_CURRENCY_SYMBOLS = {
    "AED": "AED",
    "AUD": "A$",
    "BRL": "R$",
    "CAD": "C$",
    "CHF": "CHF",
    "DKK": "kr",
    "EUR": "€",
    "GBP": "£",
    "INR": "₹",
    "JPY": "¥",
    "MXN": "MX$",
    "NOK": "kr",
    "NZD": "NZ$",
    "SEK": "kr",
    "SGD": "S$",
    "USD": "$",
    "ZAR": "R",
}


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_market(value: object) -> str | None:
    cleaned = _clean_text(value).lower()
    if cleaned and _MARKET_RE.match(cleaned):
        return cleaned
    return None


def _split_codes(raw: object) -> list[str]:
    return [part.strip().upper() for part in str(raw or "").split(",") if part.strip()]


def _format_duration_text(minutes: int) -> str:
    if minutes <= 0:
        return ""
    hours, rem = divmod(minutes, 60)
    if hours and rem:
        return f"{hours}h {rem:02d}m"
    if hours:
        return f"{hours}h"
    return f"{rem}m"


def _stop_label_for_count(stops: int) -> str:
    if stops <= 0:
        return "Direct"
    if stops == 1:
        return "1 Stop"
    return f"{stops} Stops"


def _parse_float_header(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


_TRANSIENT_PROXY_STATUS_CODES = {502, 503, 504}
_TRANSIENT_PROXY_MAX_ATTEMPTS = 3


def _extract_body_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            err_type = _clean_text(data.get("ErrorType") or data.get("errorType"))
            status_code = data.get("StatusCode") or data.get("statusCode")
            for key in ("message", "error", "detail", "Message", "Error"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    msg = val.strip()
                    return f"{err_type}: {msg}" if err_type and err_type not in msg else msg
                if isinstance(val, list):
                    parts = [str(item).strip() for item in val if str(item).strip()]
                    if parts:
                        joined = "; ".join(parts)
                        if err_type and status_code:
                            prefix = f"{err_type} (HTTP {status_code}): "
                        elif err_type:
                            prefix = f"{err_type}: "
                        else:
                            prefix = ""
                        return f"{prefix}{joined}"
            if err_type:
                return f"{err_type} (HTTP {status_code})" if status_code else err_type
    except ValueError:
        pass
    return _clean_text(response.text)[:300]


def _is_quota_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "no credit",
            "no more credit",
            "not enough credit",
            "insufficient credit",
            "quota",
            "monthly request limit",
            "limit reached",
            "subscription expired",
        )
    )


def _is_auth_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "invalid token",
            "invalid api key",
            "missing token",
            "unauthorized",
            "token is required",
            "forbidden",
        )
    )


def _looks_like_security_check(text: str) -> bool:
    lowered = text.lower()
    return (
        "/security/check" in lowered
        or "confirm that you are a real kayak user" in lowered
        or "checking your browser" in lowered
        or ("security check" in lowered and "<html" in lowered)
    )


def _is_non_flight_segment(seg_obj: dict[str, Any], airline_code: str, airline_name: str) -> bool:
    if airline_code in _NON_FLIGHT_AIRLINE_CODES:
        return True
    haystack = " ".join(
        str(part or "")
        for part in (
            airline_name,
            seg_obj.get("equipmentTypeName"),
            seg_obj.get("operationalDisplay"),
        )
    )
    return bool(_NON_FLIGHT_RE.search(haystack))


def _format_price_text(price: float, currency: str) -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    rounded = int(round(price))
    return f"{symbol}{rounded:,}" if abs(price - rounded) < 0.01 else f"{symbol}{price:,.2f}"


class ScrapeDoProvider:
    """
    KAYAK flight collection via Scrape.do's direct non-rendered POST /flights/poll API.

    Instead of loading a heavy browser page (`render=true`), this executes KAYAK's
    `/i/api/search/dynamic/flights/poll` JSON flow directly with `render=false` and
    `super=true` residential proxies geo-targeted to the group's market (`geoCode`):
      1. POST without `searchId` -> creates the KAYAK search and returns `searchId`.
      2. Sleep `initial_poll_delay_seconds` (so KAYAK finishes aggregating in parallel),
         then POST with `searchId` + `filterParams.fs` until `status == "complete"`.
    """

    name = "scrapedo"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 90,
        max_retries: int = 1,
        concurrency_limit: int = 3,
        min_delay_seconds: float = 0.5,
        quota_cooldown_seconds: int = 3600,
        country_code: str = "",
        super_proxy: bool = True,
        initial_poll_delay_seconds: float = 45.0,
        poll_delay_seconds: float = 15.0,
        max_polls: int = 1,
        allow_mult_fallback: bool = True,
        user_agent: str = "flight-harvester/1.0",
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/") or _DEFAULT_BASE_URL
        self._timeout = max(15, int(timeout))
        self._max_retries = max(1, int(max_retries))
        self._country_code = _clean_text(country_code).lower()
        self._super_proxy = bool(super_proxy)
        self._initial_poll_delay_seconds = max(0.0, float(initial_poll_delay_seconds))
        self._poll_delay_seconds = max(0.5, float(poll_delay_seconds))
        self._max_polls = max(1, int(max_polls))
        self._allow_mult_fallback = bool(allow_mult_fallback)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(float(self._timeout), connect=25.0),
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent.strip() or "flight-harvester/1.0",
            },
        )
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency_limit)))
        self._throttle_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._min_delay_seconds = max(0.0, float(min_delay_seconds))
        self._quota_blocked_until = 0.0
        self._quota_cooldown_seconds = int(quota_cooldown_seconds)
        self._last_remaining_credits: int | None = None

    def is_configured(self) -> bool:
        return bool(self._api_key and self._base_url)

    async def close(self) -> None:
        await self._client.aclose()

    def _quota_blocked(self) -> bool:
        return monotonic() < self._quota_blocked_until

    def _trip_quota_breaker(self) -> None:
        self._quota_blocked_until = monotonic() + self._quota_cooldown_seconds

    async def _wait_for_slot(self) -> None:
        async with self._throttle_lock:
            now = monotonic()
            wait_for = self._next_request_at - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._next_request_at = monotonic() + self._min_delay_seconds

    @asynccontextmanager
    async def _request_slot(self):
        async with self._semaphore:
            await self._wait_for_slot()
            yield

    def _market_country_code(
        self,
        requested_currency: str | None = None,
        requested_market: str | None = None,
    ) -> str:
        normalized_market = _normalize_market(requested_market)
        if normalized_market:
            return normalized_market

        if self._country_code:
            return self._country_code

        normalized_currency = _clean_text(requested_currency).upper()
        if normalized_currency:
            mapped = _COUNTRY_CODE_BY_CURRENCY.get(normalized_currency)
            if mapped:
                return mapped
        return "us"

    def _proxy_geo_code(
        self,
        requested_currency: str | None = None,
        requested_market: str | None = None,
    ) -> str:
        if self._country_code:
            raw = self._country_code
        else:
            raw = self._market_country_code(requested_currency, requested_market)
        return _PROXY_GEO_ALIASES.get(raw, raw)

    def _kayak_site_base(
        self,
        requested_currency: str | None = None,
        requested_market: str | None = None,
    ) -> str:
        market_country_code = self._market_country_code(requested_currency, requested_market)
        host = _KAYAK_HOST_BY_COUNTRY.get(market_country_code, _KAYAK_DEFAULT_HOST)
        return f"https://{host}"

    def _build_fs_string(
        self,
        max_stops: int | None,
        *,
        same_airline: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> str:
        filters: list[str] = []
        direct_only = max_stops is not None and max_stops <= 0
        if same_airline and not direct_only:
            filters.append("airlines=-MULT,flylocal")
        filters.append("baditin=baditin")
        if max_stops is not None and max_stops <= 1:
            filters.append(f"stops={'0' if max_stops <= 0 else '0,1'}")
        if isinstance(max_layover_minutes, int) and max_layover_minutes > 0:
            filters.append(f"layoverdur=-{max_layover_minutes}")
        if isinstance(max_leg_duration_minutes, int) and max_leg_duration_minutes > 0:
            filters.append(f"legdur=-{max_leg_duration_minutes}")
        return ";".join(filters)

    def _build_deep_link(
        self,
        *,
        base_url: str,
        trip_type: str,
        poll_legs: list[dict[str, Any]],
        fs_str: str,
    ) -> str:
        query = "?sort=price_a" + (f"&fs={fs_str}" if fs_str else "")
        if trip_type == "round_trip" and len(poll_legs) >= 2:
            out_orig = ",".join(poll_legs[0]["origin"]["airports"])
            out_dest = ",".join(poll_legs[0]["destination"]["airports"])
            dep_date = poll_legs[0]["date"]
            ret_date = poll_legs[1]["date"]
            return f"{base_url}/flights/{out_orig}-{out_dest}/{dep_date}/{ret_date}{query}"

        parts = []
        for leg in poll_legs:
            orig = ",".join(leg["origin"]["airports"])
            dest = ",".join(leg["destination"]["airports"])
            parts.append(f"{orig}-{dest}/{leg['date']}")
        return f"{base_url}/flights/" + "/".join(parts) + query

    def _build_poll_payload(
        self,
        poll_legs: list[dict[str, Any]],
        *,
        fs_str: str,
        search_id: str | None = None,
        adults: int = 1,
    ) -> dict[str, Any]:
        pax_count = max(1, int(adults))
        user_search: dict[str, Any] = {
            "legs": poll_legs,
            "pageType": "results",
            "passengers": ["ADT"] * pax_count,
            "passengerDetails": [{"ptc": "ADT"} for _ in range(pax_count)],
            "sortMode": "price_a",
        }
        if search_id:
            user_search["searchId"] = search_id
        return {
            "filterParams": {"fs": fs_str} if fs_str else {},
            "userSearchParams": user_search,
            "searchMetaData": {"pageNumber": 1, "searchTypes": []},
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        message = _extract_body_message(response) or "Scrape.do request failed."

        if response.status_code == 402:
            self._trip_quota_breaker()
            raise ProviderQuotaExhaustedError(message)

        if response.status_code == 401:
            if _is_quota_message(message):
                self._trip_quota_breaker()
                raise ProviderQuotaExhaustedError(message)
            raise ProviderAuthError(message)

        if response.status_code == 403:
            if _is_quota_message(message):
                self._trip_quota_breaker()
                raise ProviderQuotaExhaustedError(message)
            raise ProviderAuthError(message)

        if response.status_code == 429:
            raise ProviderRateLimitedError(message or "Scrape.do rate limit hit.")

        if response.status_code == 400 and _is_auth_message(message):
            raise ProviderAuthError(message)

        if response.status_code >= 400:
            if _is_quota_message(message):
                self._trip_quota_breaker()
                raise ProviderQuotaExhaustedError(message)
            if _looks_like_security_check(response.text):
                raise ProviderBlockedError(
                    "KAYAK served a security-check page instead of flight results."
                )
            raise RuntimeError(message)

    async def _post_poll(
        self,
        *,
        target_poll_url: str,
        geo_code: str,
        payload: dict[str, Any],
        step_name: str,
    ) -> tuple[dict[str, Any], float]:
        if self._quota_blocked():
            raise ProviderQuotaExhaustedError("Scrape.do quota cooldown active.")

        params: dict[str, str] = {
            "token": self._api_key,
            "url": target_poll_url,
            "super": "true" if self._super_proxy else "false",
        }
        if geo_code:
            params["geoCode"] = geo_code

        response: httpx.Response | None = None
        last_exc: Exception | None = None
        for proxy_attempt in range(1, _TRANSIENT_PROXY_MAX_ATTEMPTS + 1):
            try:
                async with self._request_slot():
                    response = await self._client.post(
                        f"{self._base_url}/", params=params, json=payload
                    )
            except httpx.RequestError as exc:
                last_exc = exc
                if proxy_attempt >= _TRANSIENT_PROXY_MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Scrape.do network error on {step_name}: {exc}"
                    ) from exc
                await asyncio.sleep(1.5 * proxy_attempt)
                continue

            if (
                response.status_code in _TRANSIENT_PROXY_STATUS_CODES
                and proxy_attempt < _TRANSIENT_PROXY_MAX_ATTEMPTS
            ):
                body_msg = _extract_body_message(response)
                if not _is_quota_message(body_msg) and not _looks_like_security_check(
                    response.text
                ):
                    log.debug(
                        "scrapedo_transient_proxy_retry",
                        step=step_name,
                        status_code=response.status_code,
                        attempt=proxy_attempt,
                        message=body_msg[:160],
                    )
                    await asyncio.sleep(1.5 * proxy_attempt)
                    continue
            break

        if response is None:
            raise RuntimeError(
                f"Scrape.do request failed on {step_name}: {last_exc or 'no response'}"
            )

        rem_credits = _parse_float_header(response.headers.get("Scrape.do-Remaining-Credits"))
        if rem_credits is not None:
            self._last_remaining_credits = int(rem_credits)
        req_cost = _parse_float_header(response.headers.get("Scrape.do-Request-Cost")) or 0.0

        self._raise_for_status(response)

        if _looks_like_security_check(response.text):
            raise ProviderBlockedError(
                "KAYAK served a security-check page instead of flight results."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Scrape.do returned invalid JSON on {step_name}: {response.text[:160]}"
            ) from exc

        if not isinstance(data, dict):
            raise RuntimeError(f"Scrape.do returned non-object JSON on {step_name}.")

        raw_res_list = data.get("results")
        log.debug(
            "scrapedo_poll_step",
            step=step_name,
            target_url=target_poll_url,
            geo_code=geo_code,
            status=data.get("status"),
            results_count=len(raw_res_list) if isinstance(raw_res_list, list) else 0,
            total_count=data.get("totalCount"),
            cost=req_cost,
            remaining_credits=self._last_remaining_credits,
        )
        return data, req_cost

    def _parse_poll_offers(
        self,
        data: dict[str, Any],
        *,
        requested_currency: str,
        market_country_code: str,
        deep_link: str,
        trip_type: str,
        expected_leg_count: int,
        max_layover_minutes: int | None,
    ) -> tuple[list[ProviderResult], int]:
        """Convert KAYAK /flights/poll JSON into canonical ProviderResult entries.

        Returns `(results, filtered_by_layover_or_nonflight)`.
        """
        raw_results = data.get("results") if isinstance(data.get("results"), list) else []
        legs_map = data.get("legs") if isinstance(data.get("legs"), dict) else {}
        segments_map = data.get("segments") if isinstance(data.get("segments"), dict) else {}
        airlines_map = data.get("airlines") if isinstance(data.get("airlines"), dict) else {}
        fallback_currency = (
            _clean_text(requested_currency).upper()
            or _CURRENCY_BY_COUNTRY.get(market_country_code, "USD")
        )

        parsed: list[ProviderResult] = []
        pruned_count = 0

        for res in raw_results:
            if not isinstance(res, dict):
                continue
            rtype = str(res.get("type") or "").strip().lower()
            if rtype in {"inlinead", "inlinedisplayad", "inlinepricealertitem", "ad"}:
                continue

            booking_options = res.get("bookingOptions")
            res_legs = res.get("legs")
            if (
                not isinstance(booking_options, list)
                or not isinstance(res_legs, list)
                or not booking_options
            ):
                continue

            valid_bookings: list[tuple[float, str, dict[str, Any]]] = []
            for opt in booking_options:
                if not isinstance(opt, dict):
                    continue
                dp = opt.get("displayPrice") if isinstance(opt.get("displayPrice"), dict) else {}
                raw_price = dp.get("price")
                if isinstance(raw_price, (int, float)) and raw_price > 0:
                    cur = _clean_text(dp.get("currency")).upper() or fallback_currency
                    valid_bookings.append((float(raw_price), cur, opt))
            if not valid_bookings:
                continue

            price, actual_currency, best_bo = min(valid_bookings, key=lambda item: item[0])
            raw_booking_url = best_bo.get("bookingUrl")
            booking_url_obj = raw_booking_url if isinstance(raw_booking_url, dict) else {}
            booking_href = _clean_text(booking_url_obj.get("url"))

            normalized_legs: list[dict[str, Any]] = []
            leg_stop_counts: list[int] = []
            leg_durations: list[int] = []
            unique_airlines: list[str] = []
            non_flight_detected = False
            max_offer_layover = 0

            for leg_ref in res_legs:
                leg_id = (
                    leg_ref.get("id")
                    if isinstance(leg_ref, dict) and leg_ref.get("id")
                    else (leg_ref if isinstance(leg_ref, str) else None)
                )
                mapped_leg = legs_map.get(leg_id) if isinstance(leg_id, str) else None
                leg_obj = (
                    mapped_leg
                    if isinstance(mapped_leg, dict)
                    else (leg_ref if isinstance(leg_ref, dict) else None)
                )
                if not isinstance(leg_obj, dict):
                    continue

                raw_dur = leg_obj.get("duration")
                leg_dur = int(raw_dur) if isinstance(raw_dur, (int, float)) and raw_dur > 0 else 0
                leg_durations.append(leg_dur)

                raw_segs = leg_obj.get("segments")
                seg_refs = raw_segs if isinstance(raw_segs, list) else []
                if not seg_refs and isinstance(leg_ref, dict):
                    inline_segs = leg_ref.get("segments")
                    if isinstance(inline_segs, list):
                        seg_refs = inline_segs
                leg_stops = max(0, len(seg_refs) - 1)
                leg_stop_counts.append(leg_stops)

                leg_airline_names: list[str] = []
                seg_origins: list[str] = []
                seg_dests: list[str] = []

                for seg_ref in seg_refs:
                    seg_id = seg_ref.get("id") if isinstance(seg_ref, dict) else str(seg_ref)
                    is_inline_seg = isinstance(seg_ref, dict) and (
                        "airline" in seg_ref or "origin" in seg_ref
                    )
                    seg_obj = seg_ref if is_inline_seg else segments_map.get(seg_id)
                    carrier_code = ""
                    if isinstance(seg_obj, dict):
                        carrier_code = str(
                            seg_obj.get("airline")
                            or seg_obj.get("carrierCode")
                            or seg_obj.get("operatingAirline")
                            or ""
                        ).strip().upper()
                        if seg_obj.get("origin"):
                            seg_origins.append(str(seg_obj["origin"]).strip().upper())
                        if seg_obj.get("destination"):
                            seg_dests.append(str(seg_obj["destination"]).strip().upper())
                    if not carrier_code and isinstance(seg_id, str):
                        match = _POLL_SEGMENT_CARRIER_RE.match(seg_id)
                        if match:
                            carrier_code = match.group(1)

                    raw_carrier = ""
                    if carrier_code and isinstance(airlines_map.get(carrier_code), dict):
                        raw_carrier = _clean_text(airlines_map[carrier_code].get("name"))
                    display_carrier = normalize_airline(raw_carrier or carrier_code)
                    if display_carrier and display_carrier != "-":
                        if display_carrier not in leg_airline_names:
                            leg_airline_names.append(display_carrier)
                        if display_carrier not in unique_airlines:
                            unique_airlines.append(display_carrier)

                    if isinstance(seg_obj, dict) and _is_non_flight_segment(
                        seg_obj, carrier_code, raw_carrier or display_carrier
                    ):
                        non_flight_detected = True

                    if isinstance(seg_ref, dict) and isinstance(seg_ref.get("layover"), dict):
                        ld = seg_ref["layover"].get("duration")
                        if isinstance(ld, (int, float)) and ld > 0:
                            max_offer_layover = max(max_offer_layover, int(ld))

                for lo in leg_obj.get("layovers") or []:
                    if isinstance(lo, dict):
                        ld = lo.get("duration")
                        if isinstance(ld, (int, float)) and ld > 0:
                            max_offer_layover = max(max_offer_layover, int(ld))

                actual_origin = seg_origins[0] if seg_origins else ""
                actual_destination = seg_dests[-1] if seg_dests else ""
                route_text = (
                    f"{actual_origin}-{actual_destination}"
                    if actual_origin and actual_destination
                    else ""
                )
                duration_text = _format_duration_text(leg_dur)
                stops_text = _stop_label_for_count(leg_stops)

                normalized_legs.append(
                    {
                        "airline": (
                            leg_airline_names[0]
                            if len(leg_airline_names) == 1
                            else " / ".join(leg_airline_names)
                        ),
                        "route_text": route_text,
                        "actual_origin": actual_origin,
                        "actual_destination": actual_destination,
                        "stops": leg_stops,
                        "stops_text": stops_text,
                        "duration_text": duration_text,
                        "duration_minutes": leg_dur,
                    }
                )

            if len(normalized_legs) < expected_leg_count:
                continue

            if non_flight_detected:
                pruned_count += 1
                continue

            if max_layover_minutes and max_offer_layover > max_layover_minutes:
                pruned_count += 1
                continue

            total_stops = sum(leg_stop_counts)
            total_duration = sum(leg_durations)
            display_airline = " / ".join(unique_airlines) if unique_airlines else "Unknown airline"
            stop_result_label = (
                " / ".join(_stop_label_for_count(count) for count in leg_stop_counts)
                if expected_leg_count > 1
                else _stop_label_for_count(total_stops)
            )
            price_text = _format_price_text(price, actual_currency)

            raw_data: dict[str, Any] = {
                "trip_type": trip_type,
                "duration_text": " / ".join(
                    leg["duration_text"]
                    for leg in normalized_legs
                    if isinstance(leg.get("duration_text"), str) and leg["duration_text"]
                ),
                "price_text": price_text,
                "final_price": price,
                "booking_href": booking_href,
                "provider_code": _clean_text(best_bo.get("providerCode")),
                "legs": normalized_legs,
                "airline_names": unique_airlines,
                "leg_stops": leg_stop_counts,
                "leg_durations": leg_durations,
                "max_layover_minutes": max_offer_layover or None,
                "stop_result_label": stop_result_label,
                "poll_status": data.get("status"),
                "poll_search_id": data.get("searchId"),
                "poll_filtered_count": data.get("filteredCount"),
                "poll_total_count": data.get("totalCount"),
            }

            actual_out_orig = _clean_text(normalized_legs[0].get("actual_origin"))
            actual_out_dest = _clean_text(normalized_legs[0].get("actual_destination"))
            if actual_out_orig:
                raw_data["actual_outbound_origin"] = actual_out_orig
            if actual_out_dest:
                raw_data["actual_outbound_destination"] = actual_out_dest
            if len(normalized_legs) > 1:
                actual_ret_orig = _clean_text(normalized_legs[-1].get("actual_origin"))
                actual_ret_dest = _clean_text(normalized_legs[-1].get("actual_destination"))
                if actual_ret_orig:
                    raw_data["actual_return_origin"] = actual_ret_orig
                if actual_ret_dest:
                    raw_data["actual_return_destination"] = actual_ret_dest

            if trip_type in {"round_trip", "multi_city"}:
                out_airline = _clean_text(normalized_legs[0].get("airline")) or display_airline
                ret_airline = (
                    _clean_text(normalized_legs[-1].get("airline"))
                    if len(normalized_legs) > 1
                    else out_airline
                ) or out_airline
                raw_data["outbound_airline"] = out_airline
                raw_data["return_airline"] = ret_airline

            parsed.append(
                ProviderResult(
                    price=price,
                    currency=actual_currency,
                    airline=display_airline,
                    deep_link=deep_link,
                    provider=self.name,
                    duration_minutes=total_duration,
                    stops=total_stops,
                    raw_data=raw_data,
                )
            )

        parsed.sort(
            key=lambda item: (
                item.price,
                item.duration_minutes if item.duration_minutes > 0 else 10**9,
                item.stops,
            )
        )
        return parsed, pruned_count

    def _filter_results_by_constraints(
        self,
        results: list[ProviderResult],
        *,
        max_stops: int | None,
        same_airline_only: bool,
        max_leg_duration_minutes: int | None,
    ) -> list[ProviderResult]:
        filtered: list[ProviderResult] = []
        for result in results:
            raw = result.raw_data if isinstance(result.raw_data, dict) else {}
            leg_stops = raw.get("leg_stops")
            if max_stops is not None:
                if not isinstance(leg_stops, list) or not leg_stops:
                    if result.stops > max_stops:
                        continue
                elif not all(int(s) <= max_stops for s in leg_stops):
                    continue

            if max_leg_duration_minutes:
                leg_durs = raw.get("leg_durations")
                if not isinstance(leg_durs, list) or not leg_durs:
                    continue
                if not all(
                    isinstance(d, (int, float)) and 0 < int(d) <= max_leg_duration_minutes
                    for d in leg_durs
                ):
                    continue

            if same_airline_only:
                raw_names = raw.get("airline_names")
                names = raw_names if isinstance(raw_names, list) else []
                keys = {str(name).strip().casefold() for name in names if str(name).strip()}
                if len(keys) != 1:
                    continue

            filtered.append(result)
        return filtered

    async def _search_poll_diagnostic(
        self,
        *,
        trip_type: str,
        poll_legs: list[dict[str, Any]],
        requested_market: str | None,
        requested_currency: str,
        max_stops: int | None,
        same_airline_only: bool,
        max_leg_duration_minutes: int | None,
        max_layover_minutes: int | None,
        adults: int = 1,
    ) -> ProviderSearchOutcome:
        if self._quota_blocked():
            raise ProviderQuotaExhaustedError("Scrape.do quota cooldown active.")

        same_airline_only = bool(same_airline_only)
        market_country_code = self._market_country_code(requested_currency, requested_market)
        geo_code = self._proxy_geo_code(requested_currency, requested_market)
        base_url = self._kayak_site_base(requested_currency, requested_market)
        target_poll_url = f"{base_url}/i/api/search/dynamic/flights/poll"

        fs_str = self._build_fs_string(
            max_stops,
            same_airline=same_airline_only,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        deep_link = self._build_deep_link(
            base_url=base_url,
            trip_type=trip_type,
            poll_legs=poll_legs,
            fs_str=fs_str,
        )

        total_credits = 0.0
        start_payload = self._build_poll_payload(poll_legs, fs_str=fs_str, adults=adults)
        final_data, start_cost = await self._post_poll(
            target_poll_url=target_poll_url,
            geo_code=geo_code,
            payload=start_payload,
            step_name="start",
        )
        total_credits += start_cost

        search_id = final_data.get("searchId") or (
            final_data.get("searchInfo", {}).get("searchId")
            if isinstance(final_data.get("searchInfo"), dict)
            else None
        )
        if not search_id:
            raise RuntimeError("KAYAK poll start did not return a searchId.")

        final_status = str(final_data.get("status") or "").lower()

        if final_status != "complete":
            for poll_idx in range(1, self._max_polls + 1):
                wait_sec = (
                    self._initial_poll_delay_seconds
                    if poll_idx == 1 and self._initial_poll_delay_seconds > 0
                    else self._poll_delay_seconds
                )
                if wait_sec > 0:
                    await asyncio.sleep(wait_sec)
                poll_payload = self._build_poll_payload(
                    poll_legs,
                    fs_str=fs_str,
                    search_id=str(search_id),
                    adults=adults,
                )
                final_data, poll_cost = await self._post_poll(
                    target_poll_url=target_poll_url,
                    geo_code=geo_code,
                    payload=poll_payload,
                    step_name=f"poll_{poll_idx}",
                )
                total_credits += poll_cost
                final_status = str(final_data.get("status") or "").lower()
                if final_status == "complete":
                    break

        if final_status != "complete":
            raise RuntimeError(
                f"KAYAK poll did not complete after {self._max_polls} polls "
                f"(status={final_status or 'unknown'})."
            )

        parsed, pruned_count = self._parse_poll_offers(
            final_data,
            requested_currency=requested_currency,
            market_country_code=market_country_code,
            deep_link=deep_link,
            trip_type=trip_type,
            expected_leg_count=len(poll_legs),
            max_layover_minutes=max_layover_minutes,
        )
        eligible = self._filter_results_by_constraints(
            parsed,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            max_leg_duration_minutes=max_leg_duration_minutes,
        )

        used_mult_fallback = False
        # Re-use the same KAYAK searchId without `airlines=-MULT,flylocal` (10 cr,
        # no second start call) when -MULT excluded all valid single-carrier fares.
        if (
            self._allow_mult_fallback
            and not eligible
            and same_airline_only
            and "airlines=-MULT" in fs_str
            and str(final_data.get("status") or "").lower() == "complete"
        ):
            used_mult_fallback = True
            fallback_fs = self._build_fs_string(
                max_stops,
                same_airline=False,
                max_leg_duration_minutes=max_leg_duration_minutes,
                max_layover_minutes=max_layover_minutes,
            )
            fallback_payload = self._build_poll_payload(
                poll_legs,
                fs_str=fallback_fs,
                search_id=str(search_id),
                adults=adults,
            )
            fallback_data, fallback_cost = await self._post_poll(
                target_poll_url=target_poll_url,
                geo_code=geo_code,
                payload=fallback_payload,
                step_name="poll_mult_fallback",
            )
            total_credits += fallback_cost
            fallback_status = str(fallback_data.get("status") or "").lower()
            if fallback_status != "complete":
                raise RuntimeError(
                    "KAYAK -MULT fallback poll did not complete "
                    f"(status={fallback_status or 'unknown'})."
                )
            fb_parsed, fb_pruned = self._parse_poll_offers(
                fallback_data,
                requested_currency=requested_currency,
                market_country_code=market_country_code,
                deep_link=deep_link,
                trip_type=trip_type,
                expected_leg_count=len(poll_legs),
                max_layover_minutes=max_layover_minutes,
            )
            fb_eligible = self._filter_results_by_constraints(
                fb_parsed,
                max_stops=max_stops,
                same_airline_only=same_airline_only,
                max_leg_duration_minutes=max_leg_duration_minutes,
            )
            if fb_eligible or len(fb_parsed) > len(parsed):
                final_data = fallback_data
                parsed = fb_parsed
                pruned_count = max(pruned_count, fb_pruned)
                eligible = fb_eligible

        for res in parsed:
            if isinstance(res.raw_data, dict):
                res.raw_data["poll_credits_cost"] = round(total_credits, 2)

        raw_total_count = final_data.get("totalCount")
        total_count_int = int(raw_total_count) if isinstance(raw_total_count, (int, float)) else 0
        raw_offers_found = max(len(parsed) + pruned_count, total_count_int)
        detected_currencies = sorted(
            {
                str(item.currency).strip().upper()
                for item in (parsed or [])
                if str(item.currency).strip()
            }
        )

        result_reason: str | None = None
        if eligible:
            result_reason = "success"
        elif not parsed and (pruned_count > 0 or total_count_int > 0):
            result_reason = "filtered_out"

        diagnostics = ProviderSearchDiagnostics(
            result_reason=result_reason,
            raw_offers_found=raw_offers_found,
            eligible_offers_found=len(eligible),
            visible_results_found=bool(parsed),
            summary_price_found=bool(parsed),
            requested_market=requested_market,
            requested_currency=requested_currency,
            detected_currencies=detected_currencies,
            used_strong_retry=used_mult_fallback,
        )
        return ProviderSearchOutcome(results=parsed, diagnostics=diagnostics)

    def _should_retry(self, exc: BaseException) -> bool:
        return isinstance(exc, RuntimeError) and not isinstance(
            exc,
            (
                ProviderQuotaExhaustedError,
                ProviderAuthError,
                ProviderRateLimitedError,
                ProviderBlockedError,
            ),
        )

    async def _run_with_retry(self, search_coro_factory) -> ProviderSearchOutcome:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=2, max=8),
            retry=retry_if_exception_type(RuntimeError) & retry_if_exception(self._should_retry),
            reraise=True,
        ):
            with attempt:
                return await search_coro_factory()
        return ProviderSearchOutcome(results=[])

    async def search_round_trip_diagnostic(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date | None,
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        del cabin
        orig_codes = _split_codes(origin)
        dest_codes = _split_codes(destination)
        poll_legs: list[dict[str, Any]] = [
            {
                "origin": {"airports": orig_codes, "locationType": "airports"},
                "destination": {"airports": dest_codes, "locationType": "airports"},
                "date": depart_date.isoformat(),
                "flex": "exact",
            }
        ]
        if return_date is not None:
            poll_legs.append(
                {
                    "origin": {"airports": dest_codes, "locationType": "airports"},
                    "destination": {"airports": orig_codes, "locationType": "airports"},
                    "date": return_date.isoformat(),
                    "flex": "exact",
                }
            )
        return await self._run_with_retry(
            lambda: self._search_poll_diagnostic(
                trip_type="round_trip",
                poll_legs=poll_legs,
                requested_market=market,
                requested_currency=currency,
                max_stops=max_stops,
                same_airline_only=bool(same_airline_only),
                max_leg_duration_minutes=max_leg_duration_minutes,
                max_layover_minutes=max_layover_minutes,
                adults=adults,
            )
        )

    async def search_multi_city_diagnostic(
        self,
        *,
        legs: list[dict[str, object]],
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        del cabin
        if not 2 <= len(legs) <= 4:
            raise ValueError("multi_city search requires 2 to 4 legs.")

        poll_legs: list[dict[str, Any]] = []
        for leg in legs:
            out_date = leg.get("outbound_date")
            if not isinstance(out_date, date):
                raise ValueError("every multi_city leg needs an outbound_date.")
            poll_legs.append(
                {
                    "origin": {
                        "airports": _split_codes(leg.get("departure_id")),
                        "locationType": "airports",
                    },
                    "destination": {
                        "airports": _split_codes(leg.get("arrival_id")),
                        "locationType": "airports",
                    },
                    "date": out_date.isoformat(),
                    "flex": "exact",
                }
            )

        outcome = await self._run_with_retry(
            lambda: self._search_poll_diagnostic(
                trip_type="multi_city",
                poll_legs=poll_legs,
                requested_market=market,
                requested_currency=currency,
                max_stops=max_stops,
                same_airline_only=bool(same_airline_only),
                max_leg_duration_minutes=max_leg_duration_minutes,
                max_layover_minutes=max_layover_minutes,
                adults=adults,
            )
        )
        outbound = legs[0]
        inbound = legs[-1]
        outbound_date: date = outbound["outbound_date"]  # type: ignore[assignment]
        inbound_date: date = inbound["outbound_date"]  # type: ignore[assignment]
        out_orig = str(outbound["departure_id"]).upper()
        out_dest = str(outbound["arrival_id"]).upper()
        in_orig = str(inbound["departure_id"]).upper()
        in_dest = str(inbound["arrival_id"]).upper()
        for res in outcome.results:
            if isinstance(res.raw_data, dict):
                res.raw_data.update(
                    {
                        "trip_type": "multi_city",
                        "outbound": {
                            "origin": out_orig,
                            "destination": out_dest,
                            "date": outbound_date.isoformat(),
                        },
                        "inbound": {
                            "origin": in_orig,
                            "destination": in_dest,
                            "date": inbound_date.isoformat(),
                        },
                        "return_origin": in_orig,
                        "return_destination": in_dest,
                        "return_date": inbound_date.isoformat(),
                    }
                )
        return outcome

    async def search_round_trip(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date,
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> list[ProviderResult]:
        outcome = await self.search_round_trip_diagnostic(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            adults=adults,
            cabin=cabin,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        return self._filter_results_by_constraints(
            outcome.results,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
        )

    async def search_multi_city(
        self,
        legs: list[dict[str, object]],
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> list[ProviderResult]:
        outcome = await self.search_multi_city_diagnostic(
            legs=legs,
            adults=adults,
            cabin=cabin,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        return self._filter_results_by_constraints(
            outcome.results,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
        )


class ScrapeDoPoolProvider:
    """Round-robin key pool / failover wrapper matching ScrapingBeePoolProvider."""

    name = "scrapedo"

    def __init__(
        self,
        api_keys: list[str],
        **provider_kwargs: object,
    ) -> None:
        self._providers = [
            ScrapeDoProvider(api_key=api_key, **provider_kwargs)
            for api_key in api_keys
            if api_key.strip()
        ]
        self._cursor = 0

    def is_configured(self) -> bool:
        return any(provider.is_configured() for provider in self._providers)

    def _ordered_providers(self) -> list[ScrapeDoProvider]:
        if not self._providers:
            return []
        start = self._cursor % len(self._providers)
        self._cursor = (self._cursor + 1) % len(self._providers)
        return self._providers[start:] + self._providers[:start]

    async def _search_outcome_with_failover(self, search_fn) -> ProviderSearchOutcome:
        last_exc: BaseException | None = None
        for provider in self._ordered_providers():
            try:
                outcome = await search_fn(provider)
                if isinstance(outcome, ProviderSearchOutcome):
                    return outcome
                return ProviderSearchOutcome(results=list(outcome))
            except (
                ProviderQuotaExhaustedError,
                ProviderAuthError,
                ProviderRateLimitedError,
                ProviderBlockedError,
                RuntimeError,
            ) as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return ProviderSearchOutcome(results=[])

    async def search_round_trip_diagnostic(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date | None,
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        return await self._search_outcome_with_failover(
            lambda provider: provider.search_round_trip_diagnostic(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                return_date=return_date,
                adults=adults,
                cabin=cabin,
                market=market,
                currency=currency,
                max_stops=max_stops,
                same_airline_only=same_airline_only,
                max_leg_duration_minutes=max_leg_duration_minutes,
                max_layover_minutes=max_layover_minutes,
            )
        )

    async def search_multi_city_diagnostic(
        self,
        *,
        legs: list[dict[str, object]],
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        return await self._search_outcome_with_failover(
            lambda provider: provider.search_multi_city_diagnostic(
                legs=legs,
                adults=adults,
                cabin=cabin,
                market=market,
                currency=currency,
                max_stops=max_stops,
                same_airline_only=same_airline_only,
                max_leg_duration_minutes=max_leg_duration_minutes,
                max_layover_minutes=max_layover_minutes,
            )
        )

    async def search_round_trip(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date,
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> list[ProviderResult]:
        outcome = await self.search_round_trip_diagnostic(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            adults=adults,
            cabin=cabin,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        ref = self._providers[0] if self._providers else None
        if ref is None:
            return outcome.results
        return ref._filter_results_by_constraints(
            outcome.results,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
        )

    async def search_multi_city(
        self,
        legs: list[dict[str, object]],
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> list[ProviderResult]:
        outcome = await self.search_multi_city_diagnostic(
            legs=legs,
            adults=adults,
            cabin=cabin,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        ref = self._providers[0] if self._providers else None
        if ref is None:
            return outcome.results
        return ref._filter_results_by_constraints(
            outcome.results,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
        )

    async def close(self) -> None:
        for provider in self._providers:
            await provider.close()
