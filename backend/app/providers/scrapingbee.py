from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import date
from time import monotonic
from urllib.parse import urljoin

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
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderResult,
    ProviderSearchDiagnostics,
    ProviderSearchOutcome,
)
from app.utils.airline_codes import normalize_airline

log = get_logger(__name__)

_BASE_URL = "https://app.scrapingbee.com/api/v1"
_KAYAK_DEFAULT_HOST = "www.kayak.com"
_MONEY_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")
_HOURS_MINUTES_RE = re.compile(r"(?i)(\d+)\s*(?:hours|hour|hrs|hr|h)\s*(?:(\d+)\s*(?:minutes|minute|mins|min|m))?")
_MINUTES_ONLY_RE = re.compile(r"(?i)(\d+)\s*(?:minutes|minute|mins|min|m)")
_STOPS_RE = re.compile(r"(?i)\b(\d+)\s+stop(?:s)?\b")
# Actual airport pair Kayak renders on a leg, e.g. "FCO-IAD" -> ("FCO", "IAD").
_AIRPORT_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*[-–—]\s*([A-Z]{3})\b")
# Carrier code embedded in a Kayak poll-JSON segment id: 13-digit epoch ms, then
# the 2-char airline code, then the flight number (e.g. "1783072800000EK5823...").
_POLL_SEGMENT_CARRIER_RE = re.compile(r"^\d{13}([A-Z0-9]{2})")
_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
_KAYAK_HOST_BY_COUNTRY = {
    "au": "www.kayak.com.au",
    "ca": "www.ca.kayak.com",
    "de": "www.kayak.de",
    "es": "www.kayak.es",
    "fr": "www.kayak.fr",
    "gb": "www.kayak.co.uk",
    "ie": "www.kayak.ie",
    "in": "www.kayak.co.in",
    "it": "www.kayak.it",
    "jp": "www.kayak.co.jp",
    "mx": "www.kayak.com.mx",
    "nl": "www.kayak.nl",
    "nz": "www.kayak.co.nz",
    "se": "www.kayak.se",
    "sg": "www.kayak.sg",
    "ch": "www.kayak.ch",
    "uk": "www.kayak.co.uk",
    "us": "www.kayak.com",
}
_COUNTRY_CODE_BY_CURRENCY = {
    "AUD": "au",
    "CAD": "ca",
    "EUR": "ie",
    "GBP": "uk",
    "INR": "in",
    "JPY": "jp",
    "MXN": "mx",
    "NZD": "nz",
    "SGD": "sg",
    "USD": "us",
}
_CURRENCY_BY_COUNTRY = {
    "au": "AUD",
    "ca": "CAD",
    "de": "EUR",
    "es": "EUR",
    "fr": "EUR",
    "gb": "GBP",
    "ie": "EUR",
    "in": "INR",
    "it": "EUR",
    "jp": "JPY",
    "mx": "MXN",
    "nl": "EUR",
    "nz": "NZD",
    "se": "SEK",
    "sg": "SGD",
    "ch": "CHF",
    "uk": "GBP",
    "us": "USD",
}

_NON_FLIGHT_TRANSPORT_TERMS = (
    " bus",
    "bus ",
    " bus ",
    "coach",
    "train",
    "rail",
    "ferry",
    "shuttle",
    "tram",
    "subway",
    "metro",
)
_CURRENCY_BY_SYMBOL = {
    "₹": "INR",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₩": "KRW",
}
_CURRENCY_BY_PRICE_TOKEN = {
    "A$": "AUD",
    "AU$": "AUD",
    "C$": "CAD",
    "CA$": "CAD",
    "HK$": "HKD",
    "NZ$": "NZD",
    "S$": "SGD",
    "SGD$": "SGD",
    "US$": "USD",
}
_MARKET_RE = re.compile(r"^[a-z]{2}$")
_SCRAPINGBEE_COUNTRY_CODE_ALIASES = {
    "uk": "gb",
}
# Intended workflow extracts only the top few cards after filters (not all cards).
# Kayak is sorted cheapest-first with the airline facet isolated, so the cheapest
# valid same-airline fare is near the top. Smaller windows = far less DOM work and
# serialization per render = faster scrapes.
# Cards serialized by f.e(). The page is price-sorted and the cheapest eligible
# fare sits at/near the top, so the exact limit barely matters; deep==fast now (the
# only old difference was an extra scroll/settle, since removed). 40 is a wide,
# sort-independent safety margin with a still-small response.
_FAST_MULTI_CITY_CARD_LIMIT = 40
_DEEP_MULTI_CITY_CARD_LIMIT = 40
_RESULT_PRICE_SELECTOR = ".nrc6-price-section .e2GB-price-text"
_SAME_AIRLINE_INITIAL_WAIT_MS = 5_000
_SAME_AIRLINE_RETRY_WAIT_MS = 9_000
# STEPPED load gate, sized to ScrapingBee's documented ~40s JS-SCENARIO limit
# ("Your whole scenario should not take more than 40 seconds to complete, otherwise
# the API will timeout"). This is a SEPARATE, tighter ceiling than the 140s overall
# render `timeout` -- leaning on 140s (the old wait_for-stamp gate) let the SCENARIO
# run past 40s on slow routes, which is what produced the ~2m HAN Provider Errors.
#
# The gate is ScrapingBee-driven fixed `wait` steps (NOT a JS interval / open-ended
# wait_for that can overshoot): helper -> [wait 10s -> FH.s()] x _MAX_POLLS -> FH.e().
# FH.s() stamps 'settled=natural' once the TOP ELIGIBLE PRICE and the rendered CARD
# COUNT hold steady for _STABLE_CHECKS consecutive polls (earliest stamp at the 3rd
# poll, ~30s in -- an inherent >=20s observation window so a late cheaper fare can't
# be locked out). The old stability tuple also required the "N of M flights" counter
# (which STREAMS continuously, never holds two polls in a row, and is null on
# non-English locales) plus the facet floor -- together they made settling
# IMPOSSIBLE, so every render reported how=forced (bug H1). _MAX_POLLS * poll = 40s
# caps the whole scenario at the documented limit, and FH.e() ALWAYS runs last so a
# page that never settles still returns its loaded cards (how=forced) instead of
# timing out. Proven live 2026-06-10: 16/16 HTTP 200 on the HAN group that used to
# 500 at ~137s. No separate wait_for(price): the first 10s wait doubles as the
# initial load wait, keeping the total scenario inside 40s.
_LOAD_GATE_POLL_MS = 10_000
_LOAD_GATE_STABLE_CHECKS = 2
_LOAD_GATE_MAX_POLLS = 4  # 4 x 10s = 40s scenario cap (the documented js_scenario max)
# Hard cap on CHARGED (successful) ScrapingBee renders per date per run. Each
# render = 5 credits; the optional retry/fallback layers (hollow-retry, -MULT
# 0-card fallback, strong-retry, two-witness re-render) could otherwise STACK to
# ~5 charged renders = ~25 credits for a single entry on the hard HAN/long-haul
# routes -- the "~20 credits per entry / 3.5x usual" the client flagged. Capping
# the budget at 2 keeps the initial render + at most ONE corrective retry, so a
# date costs ~5-10 credits instead of 20-25. Failed (5xx) renders are free and
# are NOT counted against this budget. A date that still hasn't resolved within
# the budget falls through to its normal cross-cycle retry under the error caps.
_MAX_RENDERS_PER_SEARCH = 2
# ScrapingBee's internal render budget must stay BELOW the httpx client timeout so
# a render that legitimately uses most of its budget still returns (with proxy /
# browser-startup / response-transfer overhead) before the client gives up.
# Equal budgets caused every slow Kayak page to fail as "ScrapingBee request timed
# out" at ~the client timeout instead of returning an (inspectable) payload.
_RENDER_TIMEOUT_HEADROOM_SECONDS = 35
_MIN_RENDER_TIMEOUT_MS = 20_000
_MAX_RENDER_TIMEOUT_MS = 140_000


class _RenderBudget:
    """Per-search counter for CHARGED (successful) renders, so the layered retry/
    fallback paths can't stack into ~25 credits for one date. One instance is
    created per search (NOT shared across concurrent dates), so it is concurrency-
    safe. Only successful renders consume budget; failures are free and uncounted.
    """

    __slots__ = ("used", "cap")

    def __init__(self, cap: int) -> None:
        self.used = 0
        self.cap = max(1, cap)

    def has_room(self) -> bool:
        return self.used < self.cap

    def consume(self) -> None:
        self.used += 1


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_market(value: object) -> str | None:
    market = str(value or "").strip().lower()
    if not market:
        return None
    if not _MARKET_RE.match(market):
        raise ValueError("market must be a 2-letter country code such as us, ca, uk, or in")
    return market


def _extract_body_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        return resp.text.strip()

    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return resp.text.strip()


def _is_quota_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "no more credit",
            "not enough credit",
            "insufficient credit",
            "quota",
        )
    )


def _is_auth_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "invalid api key",
            "missing api key",
            "unauthorized",
            "forbidden",
        )
    ) 


class ScrapingBeeProvider:
    """
    KAYAK page scraping via ScrapingBee's HTML API.

    ScrapingBee handles browser rendering / proxy rotation while this provider
    builds KAYAK search URLs and normalizes the extracted offers into the
    app's provider contract.
    """

    name = "scrapingbee"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        timeout: int = 30,
        max_retries: int = 3,
        concurrency_limit: int = 2,
        rendered_concurrency_limit: int | None = None,
        min_delay_seconds: float = 1.0,
        quota_cooldown_seconds: int = 3600,
        country_code: str = "us",
        premium_proxy: bool = False,
        stealth_proxy: bool = False,
        multi_city_debug: bool = False,
        user_agent: str = "flight-harvester/1.0",
        enforce_poll_agreement: bool = False,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._country_code = country_code.strip().lower()
        self._premium_proxy = premium_proxy
        self._stealth_proxy = stealth_proxy
        self._multi_city_debug = multi_city_debug
        # Two-witness enforcement: refuse saves whose DOM price disagrees with
        # Kayak's own poll JSON after one retry. OFF = shadow mode (log only).
        self._enforce_poll_agreement = bool(enforce_poll_agreement)
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent.strip() or "flight-harvester/1.0",
            },
        )

        self._semaphore = asyncio.Semaphore(max(1, concurrency_limit))
        effective_rendered_limit = (
            concurrency_limit
            if rendered_concurrency_limit is None
            else rendered_concurrency_limit
        )
        self._rendered_semaphore = asyncio.Semaphore(max(1, effective_rendered_limit))
        self._throttle_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._min_delay_seconds = max(0.0, min_delay_seconds)
        self._quota_blocked_until = 0.0
        self._quota_cooldown_seconds = quota_cooldown_seconds

    def is_configured(self) -> bool:
        return bool(self._api_key and self._base_url)

    async def close(self) -> None:
        await self._client.aclose()

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
        return self._country_code or "us"

    def _kayak_site_base(
        self,
        requested_currency: str | None = None,
        requested_market: str | None = None,
    ) -> str:
        market_country_code = self._market_country_code(requested_currency, requested_market)
        host = _KAYAK_HOST_BY_COUNTRY.get(market_country_code, _KAYAK_DEFAULT_HOST)
        return f"https://{host}"

    def _detect_display_currency(
        self,
        price_text: object,
        *,
        requested_currency: str,
        market_country_code: str,
    ) -> str:
        raw = _clean_text(price_text)
        if raw:
            uppercase_raw = raw.upper()
            for token, currency_code in _CURRENCY_BY_PRICE_TOKEN.items():
                if token in uppercase_raw:
                    return currency_code
            code_match = _CURRENCY_CODE_RE.search(uppercase_raw)
            if code_match:
                code = code_match.group(1)
                if code in _COUNTRY_CODE_BY_CURRENCY:
                    return code
            for symbol, currency_code in _CURRENCY_BY_SYMBOL.items():
                if symbol in raw:
                    return currency_code
            if "$" in raw:
                return _CURRENCY_BY_COUNTRY.get(
                    market_country_code,
                    _clean_text(requested_currency).upper() or "USD",
                )
        requested = _clean_text(requested_currency).upper()
        if requested:
            return requested
        return _CURRENCY_BY_COUNTRY.get(market_country_code, "USD")

    def _render_budget_ms(self) -> int:
        """ScrapingBee render timeout, kept below the httpx client timeout.

        httpx waits ``self._timeout`` seconds for the whole round-trip. ScrapingBee
        needs part of that for proxy connection, browser startup and shipping the
        JSON back, so its *render* budget must leave headroom — otherwise a render
        that uses its full budget returns after httpx has already aborted.
        """
        budget_ms = (self._timeout - _RENDER_TIMEOUT_HEADROOM_SECONDS) * 1000
        return max(_MIN_RENDER_TIMEOUT_MS, min(budget_ms, _MAX_RENDER_TIMEOUT_MS))

    def _base_request_params(
        self,
        target_url: str,
        *,
        country_code: str | None = None,
    ) -> dict[str, object]:
        params: dict[str, object] = {
            "api_key": self._api_key,
            "url": target_url,
            "render_js": "True",
            "block_resources": "False",
            "block_ads": "True",
            "device": "desktop",
            "timeout": self._render_budget_ms(),
            "wait": 0,
            # 'domcontentloaded', NOT 'load': Kayak is a live SPA that keeps loading
            # in the background (streaming prices, ads, tracking), so the full 'load'
            # event may never fire -> ScrapingBee waited for it until the ~140s wall
            # and timed out BEFORE the scenario/gate ran. 'domcontentloaded' fires in
            # a few seconds, then the scenario's wait_for(price) proceeds as soon as
            # the first price card appears.
            "wait_browser": "domcontentloaded",
            "window_width": 1600,
            "window_height": 2200,
        }
        effective_country_code = _clean_text(country_code).lower() or self._country_code
        if effective_country_code:
            effective_country_code = _SCRAPINGBEE_COUNTRY_CODE_ALIASES.get(
                effective_country_code,
                effective_country_code,
            )
            params["country_code"] = effective_country_code
        if self._premium_proxy:
            params["premium_proxy"] = "True"
        if self._stealth_proxy:
            params["stealth_proxy"] = "True"
        return params

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
    async def _request_slot(self, *, rendered: bool = False):
        if rendered:
            async with self._rendered_semaphore:
                async with self._semaphore:
                    await self._wait_for_slot()
                    yield
            return

        async with self._semaphore:
            await self._wait_for_slot()
            yield

    def _raise_for_status(self, response: httpx.Response) -> None:
        message = _extract_body_message(response) or "ScrapingBee request failed."

        if response.status_code == 401:
            self._trip_quota_breaker()
            raise ProviderQuotaExhaustedError(message)

        if response.status_code == 403:
            raise ProviderAuthError(message)

        if response.status_code == 429:
            raise ProviderRateLimitedError(message or "ScrapingBee concurrency limit hit.")

        if response.status_code == 400 and _is_auth_message(message):
            raise ProviderAuthError(message)

        if response.status_code >= 400:
            if _is_quota_message(message):
                self._trip_quota_breaker()
                raise ProviderQuotaExhaustedError(message)
            raise RuntimeError(message)

    def _tag_for_url(self, target_url: str) -> str:
        """Short request label (shown in the ScrapingBee dashboard + response
        headers) derived from the Kayak route path, e.g.
        'YEG-HAN_260703_SAI-YEG_260718'. ScrapingBee rejects tags longer than
        36 chars (HTTP 400 "Longer than maximum length 36." -- learned from a
        live probe, it is not in the docs), so dates are compacted to YYMMDD
        and the result is hard-truncated."""
        match = re.search(r"/flights/([^?]+)", target_url)
        if not match:
            return "kayak"
        path = re.sub(r"(\d{2})(\d{2})-(\d{2})-(\d{2})", r"\2\3\4", match.group(1))
        return re.sub(r"[^A-Za-z0-9_-]+", "_", path).strip("_")[:36] or "kayak"

    async def _get_rendered_payload(
        self,
        target_url: str,
        *,
        js_scenario: dict[str, object],
        country_code: str | None = None,
    ) -> dict:
        params = self._base_request_params(target_url, country_code=country_code)
        params["json_response"] = "True"
        params["js_scenario"] = json.dumps(js_scenario, separators=(",", ":"))
        params["block_resources"] = "True"
        params["tag"] = self._tag_for_url(target_url)
        # NOTE: do NOT add session_id here. Probed live 2026-06-10: session-pinned
        # requests to Kayak consistently die with net::ERR_TUNNEL_CONNECTION_FAILED
        # (HTTP 500) on this plan, while identical sessionless renders succeed --
        # ScrapingBee sessions ride a sticky proxy pool that doesn't work for this
        # target. Retries therefore get a fresh IP naturally, which is also what
        # recovers hollow renders.

        async with self._request_slot(rendered=True):
            try:
                response = await self._client.get(
                    self._base_url,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                log.warning(
                    "scrapingbee_request_timeout",
                    target_url=target_url,
                    client_timeout_s=self._timeout,
                    render_budget_ms=self._render_budget_ms(),
                )
                raise RuntimeError("ScrapingBee request timed out.") from exc
            except httpx.HTTPError as exc:
                log.warning(
                    "scrapingbee_request_failed",
                    target_url=target_url,
                    error_type=type(exc).__name__,
                )
                raise RuntimeError("ScrapingBee request failed.") from exc

        self._raise_for_status(response)

        try:
            data = await asyncio.to_thread(response.json)
        except Exception as exc:
            raise RuntimeError("ScrapingBee returned invalid rendered JSON.") from exc

        if not isinstance(data, dict):
            raise RuntimeError("ScrapingBee returned an unexpected rendered response body.")

        # Per-render observability (free: all fields ship in every json_response).
        # scenario_duration_s is the REAL js_scenario time from ScrapingBee's own
        # report -- the number the gate design argues about -- so timing regressions
        # show up in logs instead of needing probes.
        report = data.get("js_scenario_report")
        log.info(
            "scrapingbee_render",
            tag=params["tag"],
            status=data.get("initial-status-code"),
            cost=data.get("cost"),
            scenario_duration_s=(
                report.get("total_duration") if isinstance(report, dict) else None
            ),
            scenario_failures=(
                report.get("task_failure") if isinstance(report, dict) else None
            ),
        )

        return data

    def _kayak_query(
        self,
        max_stops: int | None,
        *,
        same_airline: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> str:
        """Kayak results query. The per-leg stop filter is carried in the URL
        (fs=stops=...), like Kayak's own UI, e.g. ?sort=price_a&fs=stops=0,1.

        same_airline=True (ROUND-TRIP path): also carry airlines=-MULT,flylocal in
        the URL so the page loads already isolated to single-carrier fares -- no
        in-scenario applyFacet() needed. -MULT excludes the "Multiple airlines"
        mixed bucket; flylocal is REQUIRED (re-verified 2026-06-08: -MULT alone hid
        ANA C$1,291, showing AC C$1,405 -- flylocal restores the true cheapest).
        The render path has a 0-card fallback: if the -MULT URL returns no cards
        (the historical YEG-KEF glitch), it re-renders without -MULT and the Python
        same-airline filter handles isolation.

        same_airline=False (per-group toggle OFF, or the 0-card fallback): no
        airlines token -- the cheapest itinerary qualifies regardless of carrier
        mix; Python-side filters still apply stop/duration rules.

        Quality/duration tokens (all verified honored by Kayak on the production key
        2026-06-09; values are MINUTES, form '=-MAX'):
        - baditin=baditin: ALWAYS added. It tells Kayak to SHOW longer
          itineraries, which Kayak HIDES by default. Hiding them was dropping the
          true cheapest fare on some routes (proven: YEG->FRA cheapest was 1347
          with longer hidden vs 1337 with baditin on), so we never want Kayak
          silently pruning a cheap longer flight before we see it -- with or
          without the same-airline filter.
        - layoverdur=-<min>: max layover/halt per stop. Client rule: a halt over
          ~11h makes the journey impractical (layoverdur=-660). Pairs with baditin so
          a longer flight that STILL has an acceptable halt stays eligible while the
          impractical-halt ones are cut.
        - legdur=-<min>: max single flight-leg duration (the group's Max Leg
          Duration, moved from a Python post-filter to server-side here).
        """
        filters: list[str] = []
        if same_airline:
            filters.append("airlines=-MULT,flylocal")
        # Show longer flights so a cheap longer fare is never hidden; applies
        # regardless of the same-airline toggle (the cheapest mixed-carrier fare
        # can be a "longer" one too). The layover cap keeps results practical.
        filters.append("baditin=baditin")
        if max_stops is not None and max_stops <= 1:
            filters.append(f"stops={'0' if max_stops <= 0 else '0,1'}")
        if isinstance(max_layover_minutes, int) and max_layover_minutes > 0:
            filters.append(f"layoverdur=-{max_layover_minutes}")
        if isinstance(max_leg_duration_minutes, int) and max_leg_duration_minutes > 0:
            filters.append(f"legdur=-{max_leg_duration_minutes}")
        query = "?sort=price_a"
        if filters:
            query += "&fs=" + ";".join(filters)
        return query

    def _build_search_url(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date | None = None,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_url: bool = True,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> str:
        # Round-trip carries -MULT,flylocal in the URL by default (same_airline_url).
        # The 0-card fallback rebuilds with same_airline_url=False (no -MULT).
        route = f"{origin.upper()}-{destination.upper()}"
        base_url = self._kayak_site_base(currency, market)
        query = self._kayak_query(
            max_stops,
            same_airline=same_airline_url,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        if return_date:
            return (
                f"{base_url}/flights/{route}/"
                f"{depart_date.isoformat()}/{return_date.isoformat()}{query}"
            )
        return (
            f"{base_url}/flights/{route}/"
            f"{depart_date.isoformat()}{query}"
        )

    def _build_multi_city_chain_url(
        self,
        *,
        legs: list[dict[str, object]],
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline: bool = True,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> str:
        """Kayak multi-city URL for a 2-4 leg chain:
        /flights/A-B/date1/C-D/date2[/E-F/date3[/G-H/date4]]?...
        (3- and 4-leg forms render + parse identically to 2-leg -- probed live
        2026-06-10: 50 cards each, per-leg airlines/stops intact.)"""
        base_url = self._kayak_site_base(currency, market)
        query = self._kayak_query(
            max_stops,
            same_airline=same_airline,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        parts = []
        for leg in legs:
            origin = str(leg["departure_id"]).upper()
            destination = str(leg["arrival_id"]).upper()
            leg_date: date = leg["outbound_date"]  # type: ignore[assignment]
            parts.append(f"{origin}-{destination}/{leg_date.isoformat()}")
        return f"{base_url}/flights/" + "/".join(parts) + query

    def _build_multi_city_results_url(
        self,
        *,
        outbound_origin: str,
        outbound_destination: str,
        outbound_date: date,
        inbound_origin: str,
        inbound_destination: str,
        inbound_date: date,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline: bool = True,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> str:
        # Legacy 2-leg convenience wrapper over the chain builder.
        return self._build_multi_city_chain_url(
            legs=[
                {
                    "departure_id": outbound_origin,
                    "arrival_id": outbound_destination,
                    "outbound_date": outbound_date,
                },
                {
                    "departure_id": inbound_origin,
                    "arrival_id": inbound_destination,
                    "outbound_date": inbound_date,
                },
            ],
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline=same_airline,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )

    def _build_results_scenario(
        self,
        *,
        deep: bool = False,
        same_airline_only: bool = False,
        minimum_leg_count: int = 1,
        same_airline_wait_ms: int | None = None,
        max_stops: int | None = None,
    ) -> dict[str, object]:
        # same_airline_wait_ms is a legacy knob from the old applyFacet flow;
        # kept in the signature so existing callers don't break, but unused.
        # same_airline_only now parameterizes f.top(): with the per-group toggle
        # OFF, the "top eligible" price (settle anchor + accuracy floor) is the
        # cheapest fare within the stop cap regardless of carrier mix.
        del same_airline_wait_ms
        card_limit = _DEEP_MULTI_CITY_CARD_LIMIT if deep else _FAST_MULTI_CITY_CARD_LIMIT
        # JS-side per-leg stop cap for f.top() (cheapest eligible same-airline).
        leg_stop_cap = 2 if (max_stops is None or max_stops >= 2) else (1 if max_stops == 1 else 0)

        # UNIVERSAL path (round-trip AND multi-city): the URL carries the filters
        # (sort=price_a & airlines=-MULT,flylocal [& stops]), so the page loads
        # already same-airline-isolated and Cheapest-sorted. No applyFacet()/
        # cheapest()/scroll/old-settle -- just a LEAN helper, the stepped settle
        # polls (FH.s) and the extract (FH.e). (The old applyFacet/fixed-wait flow
        # and the broken settle()/f.l() loading detector were removed -- git
        # history has them if a rollback is ever needed.)
        helper_script = (
            "(()=>{const l=__LIMIT__,g=__MIN_LEGS__,p='__PRICE_SELECTOR__',j='ol.hJSA-list > li',d='section,aside,div',q='label,button,[role=\"button\"],li',c='div[aria-label^=\"Result item\"],div[data-resultid],div.nrc6,div[class*=\"nrc6\"]',f=window.FH||(window.FH={});"
            "f.t=v=>(v||'').toString().replace(/\\s+/g,' ').trim();"
            "f.n=v=>(v||'').toString().replace(/\\u00a0/g,' ').split(/\\n+/).map(f.t).filter(Boolean);"
            "f.v=e=>{if(!e)return 0;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!='hidden'&&s.display!='none'};"
            "f.p=v=>{const m=f.t(v).replace(/,/g,'').match(/(?:[A-Z]{0,3}\\$|[$\\u20ac\\u00a3\\u20b9])\\s*([0-9]+(?:\\.[0-9]+)?)/i);return m?Number(m[1]):null};"
            "f.o=()=>Array.from(document.querySelectorAll(d)).filter(e=>f.v(e)&&/(^|\\n)\\s*Airlines\\s*($|\\n)/i.test(e.innerText||'')&&/(?:[A-Z]{0,3}\\$|[$\\u20ac\\u00a3\\u20b9])\\s*\\d/.test(e.innerText||'')).sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length)[0]||null;"
            # Cheapest visible result-card price (for the junk-facet floor below).
            "f.cp=()=>{let b=null;for(const nd of f.r()){const pr=f.p(f.t(nd.querySelector(p)?.innerText));if(pr!=null&&(b==null||pr<b))b=pr}return b};"
            # Airline facet options [{name,price}]. Junk guard: skip entries whose
            # name is empty/'book now'/payment-plan ('/mo','month','per') and entries
            # whose price is implausibly far below the cheapest result card (e.g. a
            # 'from $181/mo' financing element) -- a real airline floor is never well
            # below the cheapest visible fare.
            "f.x=()=>{const r=f.o();if(!r)return[];const cp=f.cp();const s=new Map();for(const e of Array.from(r.querySelectorAll(q+',div,span'))){if(!f.v(e))continue;const a=f.n(e.innerText);if(!a.length||a.length>3)continue;const t=a.join('|'),pr=f.p(t);if(pr===null)continue;if(cp!=null&&pr<cp*0.6)continue;let n=a.find(v=>f.p(v)===null)||'';n=f.t(n.replace(/\\b\\d+\\b/g,''));if(!n||/^(select all|clear all|show \\d+ more|book now)/i.test(n)||/(multiple airlines|mixed airlines|various airlines)/i.test(n)||/(\\/mo|month|per\\b)/i.test(t))continue;const k=n.toLowerCase(),u=s.get(k),z=e.closest(q)||e;if(!u||pr<u.p)s.set(k,{n,p:pr,e:z})}return Array.from(s.values()).sort((a,b)=>a.p-b.p).slice(0,4)};"
            "f.r=()=>Array.from(document.querySelectorAll(c)).filter(n=>n&&n.querySelector(p)&&n.querySelectorAll(j).length>=g).filter((n,i,a)=>!a.some((o,k)=>k!==i&&n.contains(o)&&o.querySelector&&o.querySelector(p)&&o.querySelectorAll(j).length>=g));"
            "f.empty=()=>!f.r().length&&/no result|no flight|no match|couldn.t f|adjust your f/i.test(document.body?.innerText||'');"
            "f.sc=v=>{v=(v||'').toLowerCase();if(!v)return null;if(/nonstop|direct/.test(v))return 0;const mm=v.match(/(\\d+)\\s*stop/);return mm?Number(mm[1]):null};"
            "f.top=()=>{let best=null;for(const nd of f.r()){const pr=f.p(f.t(nd.querySelector(p)?.innerText));if(pr==null)continue;const L=Array.from(nd.querySelectorAll(j));const air=L.map(i=>f.t(i.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt'))).filter(Boolean);const st=L.map(i=>f.sc(i.querySelector('.JWEO .vmXl')?.innerText));const kn=st.filter(x=>x!=null);const same=!__SAMEAIR__||(air.length>=2&&new Set(air.map(a=>a.toLowerCase())).size===1);const ok=kn.length>0&&kn.every(x=>x<=__MAXSTOPS__);if(same&&ok&&(best==null||pr<best))best=pr}return best};"
            # f.s (settle): ONE instant snapshot+compare. Reads the top ELIGIBLE
            # same-airline price + the rendered card count, compares to the previous
            # call's snapshot stored on f.u, and counts consecutive-stable. When both
            # hold for `need` checks it stamps the page 'settled=natural'. Stability
            # deliberately uses ONLY these two: the old "N of M flights" counter
            # streams forever (never equal across polls; null on non-English pages)
            # and the facet floor wobbles -- both made settling impossible (bug H1).
            # Returns instantly -- it CANNOT hang, so Kayak's busy thread can't
            # starve it.
            "f.s=()=>{const need=__NEED__,st=f.u||(f.u={h:0,lp:null,lc:null});const pr=f.top(),c=f.r().length;if(pr!=null&&pr===st.lp&&c===st.lc)st.h++;else st.h=0;st.lp=pr;st.lc=c;if(st.h>=need){document.body.setAttribute('data-fh-st','1');document.body.setAttribute('data-fh-how','natural')}return st.h};"
            # f.e (extract): if the page never stamped 'settled' during the poll steps,
            # this is the FORCED path -- we extract whatever is loaded at the cap and
            # mark how='forced' (vs 'natural'). e:true means a usable read either way;
            # how: tells us if it was a clean settle or a give-up-at-cap, so forced rows
            # can be cross-checked for accuracy. Always runs as its own scenario step.
            # NOTE: dropped two accuracy-irrelevant fields to free request-line bytes:
            #  - the /book/ href (h): a session-specific redirect that EXPIRES; the
            #    stable search URL is saved as the deep link instead, so h was unused.
            #  - the best/cheapest/quickest badges (b): stored but read by no selection/
            #    filter/price/stop logic. Both removals are verified non-load-bearing.
            "f.e=()=>{const stamped=document.body?.getAttribute('data-fh-st')==='1';if(!stamped){document.body.setAttribute('data-fh-st','1');document.body.setAttribute('data-fh-how','forced')}const r=f.r();return JSON.stringify({n:r.length,m:r.slice(0,l).length,c:r.slice(0,l).map(n=>({t:f.t(n.innerText),p:f.t(n.querySelector(p)?.innerText),a:f.t(n.querySelector('.J0g6-operator-text')?.innerText),l:Array.from(n.querySelectorAll(j)).slice(0,g).map(i=>({t:f.t(i.innerText),a:f.t(i.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt')),tm:f.t(i.querySelector('.VY2U .vmXl')?.innerText),r:f.t(i.querySelector('.VY2U [dir=\"ltr\"]')?.innerText),s:f.t(i.querySelector('.JWEO .vmXl')?.innerText),ly:f.t(i.querySelector('.JWEO .c_cgF')?.innerText),d:f.t(i.querySelector('.xdW8 .vmXl')?.innerText)})).filter(i=>i.t)})),f:{s:'',o:f.x().map(v=>({n:v.n,p:v.p}))},e:stamped,how:document.body?.getAttribute('data-fh-how')||'forced',np:f.empty(),sm:true})};f.settle=f.s;f.extract=f.e;return true})()"
        )
        helper_script = (
            helper_script.replace("__LIMIT__", str(card_limit))
            .replace("__MIN_LEGS__", str(max(1, minimum_leg_count)))
            .replace("__PRICE_SELECTOR__", _RESULT_PRICE_SELECTOR)
            .replace("__MAXSTOPS__", str(leg_stop_cap))
            .replace("__NEED__", str(_LOAD_GATE_STABLE_CHECKS))
            .replace("__SAMEAIR__", "true" if same_airline_only else "false")
        )

        # STEPPED load gate bounded to ScrapingBee's ~40s js_scenario limit. The
        # timing is driven by fixed `wait` steps (ScrapingBee-controlled), NOT a JS
        # interval or an open-ended wait_for -- so the scenario can NEVER overshoot
        # 40s into "the API will timeout" (the cause of the ~2m HAN Provider Errors
        # when the old wait_for-stamp gate leaned on the 140s render budget instead).
        #
        # Flow: helper -> [wait 10s -> FH.s()] x _LOAD_GATE_MAX_POLLS -> FH.e().
        # - No separate wait_for(price): the first 10s wait doubles as the initial
        #   load wait, so the whole scenario stays inside 40s (4 x 10s = 40s).
        # - FH.s() stamps 'settled=natural' once the top eligible price + card count
        #   hold steady for _LOAD_GATE_STABLE_CHECKS consecutive polls (earliest at
        #   the 3rd poll, ~30s); once stamped, later FH.s() calls are cheap no-ops.
        # - FH.e() ALWAYS runs last: if the page never stamped 'natural' it extracts
        #   whatever loaded and marks how='forced' -- returning a result instead of a
        #   timeout. "strict": False keeps any single step's failure from aborting.
        instructions: list[dict[str, object]] = [{"evaluate": helper_script}]
        for _ in range(_LOAD_GATE_MAX_POLLS):
            instructions.append({"wait": _LOAD_GATE_POLL_MS})
            instructions.append({"evaluate": "window.FH.s()"})
        instructions.append({"evaluate": "window.FH.e()"})
        return {"strict": False, "instructions": instructions}

    async def _parse_rendered_payload(
        self,
        rendered: dict,
        *,
        currency: str,
        deep_link: str,
        market_country_code: str,
        trip_type: str,
        expected_leg_count: int,
    ) -> tuple[list[ProviderResult], int, int]:
        cards_payload = self._extract_rendered_cards_payload(rendered)
        if cards_payload is None:
            fallback_results = await asyncio.to_thread(
                self._normalize_flights,
                rendered,
                currency=currency,
                deep_link=deep_link,
                trip_type=trip_type,
                market_country_code=market_country_code,
            )
            return fallback_results, len(fallback_results), len(fallback_results)

        card_count = 0
        captured_count = 0
        raw_count = cards_payload.get("card_count")
        if isinstance(raw_count, int) and raw_count >= 0:
            card_count = raw_count
        raw_captured_count = cards_payload.get("captured_count")
        if isinstance(raw_captured_count, int) and raw_captured_count >= 0:
            captured_count = raw_captured_count

        results = await asyncio.to_thread(
            self._normalize_rendered_cards,
            cards_payload,
            currency=currency,
            deep_link=deep_link,
            trip_type=trip_type,
            market_country_code=market_country_code,
            expected_leg_count=expected_leg_count,
        )
        return results, card_count, captured_count

    def _filter_results_by_stops(
        self,
        results: list[ProviderResult],
        max_stops: int | None,
    ) -> list[ProviderResult]:
        limit = self._allowed_leg_stop_limit(max_stops)
        if limit is None:
            return results

        return [
            result
            for result in results
            if all(stops <= limit for stops in self._result_leg_stops(result))
        ]

    def _allowed_leg_stop_limit(self, max_stops: int | None) -> int | None:
        if max_stops is None:
            return None
        if max_stops <= 0:
            return 0
        if max_stops == 1:
            return 1
        return 2

    def _airline_match_key(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = normalize_airline(value).strip()
        if not normalized or normalized == "-":
            return None
        return normalized.casefold()

    def _route_text_carries_airline_signal(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        cleaned = value.strip()
        if not cleaned:
            return False
        lowered = cleaned.casefold()
        if lowered in {"multiple airlines", "mixed airlines", "various airlines"}:
            return True
        return any(separator in cleaned for separator in (",", "/", ";", "|"))

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
                    route_text = leg.get("route_text")
                    if self._route_text_carries_airline_signal(route_text):
                        raw_values.append(route_text)

        raw_values.append(raw_data.get("outbound_airline"))
        raw_values.append(raw_data.get("return_airline"))
        raw_values.append(result.airline)

        names: list[str] = []
        for raw_value in raw_values:
            if not isinstance(raw_value, str):
                continue
            for token in re.split(r"\s*[/,;|]\s*", raw_value):
                normalized = normalize_airline(token).strip()
                if normalized and normalized != "-" and normalized not in names:
                    names.append(normalized)
        return names

    def _same_airline_results_only(self, results: list[ProviderResult]) -> list[ProviderResult]:
        filtered: list[ProviderResult] = []
        for result in results:
            airline_names = self._result_airline_names(result)
            airline_keys = {
                key
                for name in airline_names
                if (key := self._airline_match_key(name)) is not None
            }
            if len(airline_keys) != 1:
                continue
            if airline_names:
                result.airline = airline_names[0]
            filtered.append(result)
        return filtered

    def _eligible_same_airline_results(
        self,
        results: list[ProviderResult],
        max_stops: int | None,
        same_airline_only: bool = True,
    ) -> list[ProviderResult]:
        within_stops = self._filter_results_by_stops(results, max_stops)
        if not same_airline_only:
            return within_stops
        return self._same_airline_results_only(within_stops)

    def _diagnostics_for_results(
        self,
        *,
        results: list[ProviderResult],
        requested_market: str | None,
        requested_currency: str,
        result_reason: str | None = None,
        visible_results_found: bool = False,
        summary_price_found: bool = False,
        used_strong_retry: bool = False,
    ) -> ProviderSearchDiagnostics:
        detected_currencies = sorted(
            {
                _clean_text(result.currency).upper()
                for result in results
                if _clean_text(result.currency)
            }
        )
        return ProviderSearchDiagnostics(
            result_reason=result_reason,
            raw_offers_found=len(results),
            eligible_offers_found=len(results),
            visible_results_found=visible_results_found,
            summary_price_found=summary_price_found,
            requested_market=requested_market,
            requested_currency=requested_currency,
            detected_currencies=detected_currencies,
            used_strong_retry=used_strong_retry,
        )

    def _stop_label_for_count(self, stops: int) -> str:
        if stops <= 0:
            return "Direct"
        if stops == 1:
            return "1 Stop"
        return f"{stops} Stops"

    def _stop_label_from_leg_stops(self, leg_stops: list[int]) -> str:
        if not leg_stops:
            return ""
        return " / ".join(self._stop_label_for_count(stops) for stops in leg_stops)

    def _result_leg_stops(self, result: ProviderResult) -> list[int]:
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

    def _extract_rendered_cards_payload(self, rendered: dict) -> dict[str, object] | None:
        evaluate_results = rendered.get("evaluate_results")
        if not isinstance(evaluate_results, list) or not evaluate_results:
            return None
        for item in reversed(evaluate_results):
            if not isinstance(item, str):
                continue
            try:
                payload = json.loads(item)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("cards"), list):
                return payload
            if isinstance(payload.get("c"), list):
                return {
                    "card_count": payload.get("n", 0),
                    "captured_count": payload.get("m", 0),
                    "cards": [
                        {
                            "text": card.get("t", ""),
                            "price_text": card.get("p", ""),
                            "initial_price_text": card.get("ip", ""),
                            "booking_href": card.get("h", ""),
                            "cabin": card.get("cb", ""),
                            "airline_text": card.get("a", ""),
                            "badges": card.get("b", []),
                            "legs": [
                                {
                                    "text": leg.get("t", ""),
                                    "airline": leg.get("a", ""),
                                    "time_text": leg.get("tm", ""),
                                    "route_text": leg.get("r", ""),
                                    "stops_text": leg.get("s", ""),
                                    "layover_text": leg.get("ly", ""),
                                    "duration_text": leg.get("d", ""),
                                }
                                for leg in card.get("l", [])
                                if isinstance(leg, dict)
                            ],
                        }
                        for card in payload.get("c", [])
                        if isinstance(card, dict)
                    ],
                    "summary": {
                        "cheapest": ((payload.get("s") or {}).get("c", "")),
                        "best": ((payload.get("s") or {}).get("b", "")),
                        "quickest": ((payload.get("s") or {}).get("q", "")),
                    },
                    "facet": {
                        "selected": ((payload.get("f") or {}).get("s", "")),
                        "options": [
                            {
                                "name": option.get("n", ""),
                                "price": option.get("p"),
                            }
                            for option in ((payload.get("f") or {}).get("o") or [])
                            if isinstance(option, dict)
                        ],
                    },
                    "settled": payload.get("e"),
                    "how": payload.get("how"),
                    "no_results": payload.get("np"),
                    "same_airline_mode": payload.get("sm"),
                }
        return None

    def _rendered_payload_has_summary_prices(self, rendered: dict) -> bool:
        payload = self._extract_rendered_cards_payload(rendered)
        if payload is None:
            return False
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            return False
        return any(_clean_text(summary.get(key)) for key in ("cheapest", "best"))

    def _rendered_payload_reports_no_results(self, rendered: dict) -> bool:
        """True when Kayak explicitly rendered a 'no results' state for the route.

        Distinguishes a legitimately empty route (small/unsupported airport,
        beyond booking horizon) from a render that failed to hydrate, so empty
        routes can be classified as no_results instead of burning credit as a
        timeout.
        """
        payload = self._extract_rendered_cards_payload(rendered)
        if payload is None:
            return False
        return bool(payload.get("no_results"))

    def _looks_hollow_render(
        self,
        rendered: dict,
        results: list[ProviderResult],
        card_count: int,
    ) -> bool:
        """True when the browser session never received inventory at all.

        Signature (probed live 2026-06-10, ~1-in-10 renders): zero cards, no
        explicit Kayak 'no results' page, no summary prices, AND the captured
        poll JSON has zero results -- i.e. the session was a dud, not the date.
        A plain retry (fresh proxy IP) recovered the real fare (C$3,111 on
        2026-07-30) where the dud saw nothing. Requires xhr evidence to say 'hollow': without a
        captured xhr list (older fixtures/odd responses) we stay conservative
        and fall through to the existing fallback paths instead.
        """
        if results or card_count > 0:
            return False
        if self._rendered_payload_reports_no_results(rendered):
            return False
        if self._rendered_payload_has_summary_prices(rendered):
            return False
        if not isinstance(rendered.get("xhr"), list):
            return False
        snapshot = self._poll_snapshot(rendered, None)
        return not snapshot.get("poll_results_count")

    def _strong_retry_worthwhile(self, rendered: dict) -> bool:
        """Whether a second (slower) render is likely to recover results.

        Only retry when there's a signal that data SHOULD exist but extraction
        glitched: Kayak did NOT report 'no results', AND the page showed some
        airline facet options or summary prices. If the route is genuinely empty
        (no_results, or no facet/summary signal at all), a 2nd render just burns
        ScrapingBee credit for nothing, so we skip it.
        """
        if self._rendered_payload_reports_no_results(rendered):
            return False
        if self._facet_option_prices(rendered):
            return True
        if self._rendered_payload_has_summary_prices(rendered):
            return True
        return False

    def _render_failure_snapshot(self, rendered: dict) -> dict[str, object]:
        """Compact, secret-free signals explaining *why* a render produced no
        cards, so we can tell selector drift from a page that never hydrated.

        Never returns full HTML or any credential. The body is inspected only
        for presence of known marker substrings and a short <title>.
        """
        body = rendered.get("body")
        body_text = body if isinstance(body, str) else ""
        body_len = len(body_text)

        title = ""
        match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body_text)
        if match:
            title = _clean_text(match.group(1))[:120]

        # Cheap substring checks — does the marker class even exist in the DOM?
        # If the price selector class is absent while result containers exist,
        # that points at selector drift rather than a non-hydrated page.
        markers = {
            "price_cls": "e2GB-price-text" in body_text,
            "card_cls": "nrc6" in body_text,
            "leg_list_cls": "hJSA-list" in body_text,
            "airlines_facet": "Airlines" in body_text,
            "captcha_or_block": any(
                token in body_text.lower()
                for token in ("captcha", "are you a robot", "access denied", "unusual traffic")
            ),
        }

        snapshot: dict[str, object] = {
            "http_status": rendered.get("initial-status-code") or rendered.get("status_code"),
            "cost": rendered.get("cost"),
            "resolved_url": rendered.get("resolved-url"),
            "body_length": body_len,
            "title": title,
            "markers": markers,
            "evaluate_results_count": (
                len(rendered.get("evaluate_results"))
                if isinstance(rendered.get("evaluate_results"), list)
                else 0
            ),
        }
        return snapshot

    def _multi_city_summary_prices(self, rendered: dict) -> dict[str, str]:
        payload = self._extract_rendered_cards_payload(rendered)
        if payload is None:
            return {}
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            return {}
        prices: dict[str, str] = {}
        for key in ("cheapest", "best", "quickest"):
            value = _clean_text(summary.get(key))
            if value:
                prices[key] = value
        return prices

    def _multi_city_facet_snapshot(self, rendered: dict) -> tuple[str, int]:
        payload = self._extract_rendered_cards_payload(rendered)
        if payload is None:
            return "", 0
        facet = payload.get("facet")
        if not isinstance(facet, dict):
            return "", 0
        selected = _clean_text(facet.get("selected"))
        options = facet.get("options")
        if not isinstance(options, list):
            return selected, 0
        option_count = sum(1 for option in options if isinstance(option, dict))
        return selected, option_count

    def _facet_option_prices(self, rendered: dict) -> list[float]:
        payload = self._extract_rendered_cards_payload(rendered)
        if payload is None:
            return []
        facet = payload.get("facet")
        if not isinstance(facet, dict):
            return []
        options = facet.get("options")
        if not isinstance(options, list):
            return []

        prices: list[float] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            raw_price = option.get("price")
            price: float | None
            if isinstance(raw_price, (int, float)) and raw_price > 0:
                price = float(raw_price)
            else:
                price = self._parse_price(raw_price)
            if price is not None and price > 0:
                prices.append(price)
        return prices

    def _cheapest_result_price(self, results: list[ProviderResult]) -> float | None:
        prices = [float(result.price) for result in results if result.price is not None]
        return min(prices) if prices else None

    def _poll_snapshot(
        self,
        rendered: dict,
        max_stops: int | None,
        same_airline_only: bool = True,
    ) -> dict[str, object]:
        """SHADOW cross-check from Kayak's own /flights/poll XHR JSON (captured for
        free in every json_response and previously discarded).

        Returns Kayak's ground truth for the render: how many results it says
        match the filters (filteredCount -- when it equals the DOM card count the
        render was COMPLETE, proven on every 2026-06-10 probe), and the cheapest
        same-airline fare within the per-leg stop cap computed from the JSON
        itself (carriers parsed from segment ids, stops = segments-1). Logging
        only for now: a DOM-vs-poll disagreement is the signal a save shouldn't
        be trusted. Never raises -- diagnostics must not break a scrape.
        """
        try:
            xhr = rendered.get("xhr")
            if not isinstance(xhr, list):
                return {}
            bodies = [
                x.get("body")
                for x in xhr
                if isinstance(x, dict)
                and "flights/poll" in str(x.get("url", ""))
                and isinstance(x.get("body"), str)
            ]
            if not bodies:
                return {}
            payload = json.loads(max(bodies, key=len))
            results = payload.get("results")
            if not isinstance(results, list):
                results = []
            stop_limit = self._allowed_leg_stop_limit(max_stops)

            cheapest: float | None = None
            for result in results:
                if not isinstance(result, dict):
                    continue
                prices = [
                    option.get("displayPrice", {}).get("price")
                    for option in result.get("bookingOptions") or []
                    if isinstance(option, dict)
                ]
                prices = [p for p in prices if isinstance(p, (int, float))]
                if not prices:
                    continue
                carriers: set[str] = set()
                leg_stops: list[int] = []
                legs = result.get("legs")
                if not isinstance(legs, list) or len(legs) < 2:
                    continue
                for leg in legs:
                    segments = leg.get("segments") if isinstance(leg, dict) else None
                    if not isinstance(segments, list) or not segments:
                        carriers.clear()
                        break
                    for segment in segments:
                        match = _POLL_SEGMENT_CARRIER_RE.match(str(segment.get("id", "")))
                        if match:
                            carriers.add(match.group(1))
                    leg_stops.append(max(0, len(segments) - 1))
                if len(leg_stops) < 2:
                    continue
                if same_airline_only and len(carriers) != 1:
                    continue
                if stop_limit is not None and any(s > stop_limit for s in leg_stops):
                    continue
                price = float(min(prices))
                if cheapest is None or price < cheapest:
                    cheapest = price

            return {
                "poll_results_count": len(results),
                "poll_filtered_count": payload.get("filteredCount"),
                "poll_cheapest_eligible": cheapest,
            }
        except Exception:
            return {}

    def _accuracy_audit(
        self,
        *,
        rendered: dict,
        summary_prices: dict[str, str],
        eligible_results: list[ProviderResult],
    ) -> dict[str, object]:
        """Numbers for verifying the saved fare against Kayak's own prices.

        - saved_price: cheapest same-airline fare we are about to keep.
        - facet_floor: cheapest airline-facet price Kayak displayed.
        - summary_cheapest: Kayak's headline "Cheapest" price (across all airlines).
        - floor_gap / summary_gap: how far the saved fare sits above each, so a
          systematic over-charge (scraper missed a cheaper same-airline option)
          shows up directly in the logs without a manual browser check.
        """
        saved = self._cheapest_result_price(eligible_results)
        facet_prices = self._facet_option_prices(rendered)
        facet_floor = min(facet_prices) if facet_prices else None
        summary_cheapest = self._summary_lowest_price(summary_prices)

        def _gap(value: float | None, base: float | None) -> float | None:
            if value is None or base is None or base <= 0:
                return None
            return round(value - base, 2)

        return {
            "saved_price": saved,
            "facet_floor": facet_floor,
            "summary_cheapest": summary_cheapest,
            "floor_gap": _gap(saved, facet_floor),
            "summary_gap": _gap(saved, summary_cheapest),
        }

    def _render_quality_fields(
        self,
        *,
        rendered: dict,
        eligible_results: list[ProviderResult],
        max_stops: int | None,
        same_airline_only: bool = True,
    ) -> dict[str, object]:
        """Accuracy/trust telemetry for one render, merged into scrapingbee_results.

        - settled/how: did the gate observe a stable page (natural) or give up at
          the 40s cap (forced)? Forced reads are the ones worth auditing.
        - scenario_duration_s: ScrapingBee's own js_scenario timing.
        - poll_*: Kayak's ground truth from its poll JSON (see _poll_snapshot).
        - dom_poll_agree: True when the DOM-extracted cheapest eligible fare and
          the poll-JSON cheapest agree to the dollar (probed 14/14 on clean
          renders 2026-06-10); False is the save-shouldn't-be-trusted signal.
        """
        payload = self._extract_rendered_cards_payload(rendered) or {}
        report = rendered.get("js_scenario_report")
        snapshot = self._poll_snapshot(rendered, max_stops, same_airline_only)
        saved = self._cheapest_result_price(eligible_results)
        poll_best = snapshot.get("poll_cheapest_eligible")
        agrees: bool | None = None
        if saved is not None and isinstance(poll_best, (int, float)):
            agrees = abs(saved - float(poll_best)) < 1.0
        return {
            "settled": payload.get("settled"),
            "how": payload.get("how"),
            "scenario_duration_s": (
                report.get("total_duration") if isinstance(report, dict) else None
            ),
            **snapshot,
            "dom_poll_agree": agrees,
        }

    async def _enforce_two_witness_agreement(
        self,
        *,
        target_url: str,
        country_code: str,
        currency: str,
        trip_type: str,
        minimum_leg_count: int,
        max_stops: int | None,
        same_airline_only: bool,
        rendered: dict,
        summary_prices: dict[str, str],
        results: list[ProviderResult],
        card_count: int,
        captured_count: int,
        eligible_results: list[ProviderResult],
        retry_allowed: bool,
        budget: "_RenderBudget | None" = None,
    ) -> tuple[dict, dict[str, str], list[ProviderResult], int, int, list[ProviderResult], bool, bool]:
        """Two-witness accuracy gate (scrape_enforce_poll_agreement).

        A save is trustworthy only when the DOM-extracted cheapest eligible fare
        and Kayak's own poll JSON agree (probed 17/17 on clean renders). When
        they DISAGREE: one re-render (if the shared retry budget allows -- the
        hollow/strong retries count against it), and if the witnesses still
        disagree the price is REFUSED (caller empties eligible_results and logs
        the date extract_failed, so it retries under the normal caps instead of
        persisting a suspect price). Accuracy > coverage: a blank cell the
        client can see beats a wrong price they can't.

        Returns (rendered, summary_prices, results, card_count, captured_count,
        eligible_results, refused, retried). No-op unless enforcement is ON and
        there is something to save; dom_poll_agree=None (no poll evidence) is
        NOT treated as disagreement.
        """
        state = (rendered, summary_prices, results, card_count, captured_count, eligible_results)
        if not self._enforce_poll_agreement or not eligible_results:
            return (*state, False, False)
        quality = self._render_quality_fields(
            rendered=rendered,
            eligible_results=eligible_results,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
        )
        if quality.get("dom_poll_agree") is not False:
            return (*state, False, False)

        retried = False
        if retry_allowed:
            log.warning(
                "scrapingbee_poll_disagreement_retry",
                trip_type=trip_type,
                target_url=target_url,
                saved_price=quality.get("saved_price"),
                poll_cheapest_eligible=quality.get("poll_cheapest_eligible"),
            )
            retried = True
            rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
                target_url=target_url,
                country_code=country_code,
                currency=currency,
                trip_type=trip_type,
                minimum_leg_count=minimum_leg_count,
                deep=True,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                budget=budget,
            )
            eligible_results = self._eligible_same_airline_results(
                results, max_stops, same_airline_only
            )
            state = (rendered, summary_prices, results, card_count, captured_count, eligible_results)
            quality = self._render_quality_fields(
                rendered=rendered,
                eligible_results=eligible_results,
                max_stops=max_stops,
                same_airline_only=same_airline_only,
            )
            if not eligible_results or quality.get("dom_poll_agree") is not False:
                return (*state, False, True)

        log.warning(
            "scrapingbee_poll_disagreement_refused",
            trip_type=trip_type,
            target_url=target_url,
            saved_price=quality.get("saved_price"),
            poll_cheapest_eligible=quality.get("poll_cheapest_eligible"),
            retried=retried,
        )
        return (*state, True, retried)

    def _actual_route_label(self, eligible_results: list[ProviderResult]) -> str | None:
        """The actual airports flown on the cheapest saved fare (e.g. 'FCO->IAD'),
        so group logs reveal the real airport when a city code like ROM was searched."""
        if not eligible_results:
            return None
        cheapest = min(eligible_results, key=lambda r: r.price)
        raw = cheapest.raw_data if isinstance(cheapest.raw_data, dict) else {}
        out_o = _clean_text(raw.get("actual_outbound_origin"))
        out_d = _clean_text(raw.get("actual_outbound_destination"))
        if not out_o or not out_d:
            return None
        label = f"{out_o}->{out_d}"
        ret_o = _clean_text(raw.get("actual_return_origin"))
        ret_d = _clean_text(raw.get("actual_return_destination"))
        if ret_o and ret_d:
            label += f" / {ret_o}->{ret_d}"
        return label

    def _summary_lowest_price(self, summary_prices: dict[str, str]) -> float | None:
        prices = [
            price
            for price in (
                self._parse_price(value)
                for value in summary_prices.values()
            )
            if price is not None
        ]
        return min(prices) if prices else None

    def _log_multi_city_debug_snapshot(
        self,
        *,
        outbound_origin: str,
        outbound_destination: str,
        outbound_date: date,
        inbound_origin: str,
        inbound_destination: str,
        inbound_date: date,
        target_url: str,
        summary_prices: dict[str, str],
        card_count: int,
        captured_count: int,
        raw_results: list[ProviderResult],
        eligible_results: list[ProviderResult],
        max_stops: int | None,
        used_deep_pass: bool,
    ) -> None:
        if not self._multi_city_debug:
            return

        def _preview(items: list[ProviderResult]) -> list[dict[str, object]]:
            preview: list[dict[str, object]] = []
            for result in items[:10]:
                raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
                legs = raw_data.get("legs")
                normalized_legs = legs if isinstance(legs, list) else []
                outbound_leg = normalized_legs[0] if len(normalized_legs) > 0 and isinstance(normalized_legs[0], dict) else {}
                inbound_leg = normalized_legs[1] if len(normalized_legs) > 1 and isinstance(normalized_legs[1], dict) else {}
                preview.append(
                    {
                        "price": result.price,
                        "airline": result.airline,
                        "stops": result.stops,
                        "duration_minutes": result.duration_minutes,
                        "price_text": _clean_text(raw_data.get("price_text")),
                        "badges": raw_data.get("badges") if isinstance(raw_data.get("badges"), list) else [],
                        "outbound_time": _clean_text(outbound_leg.get("time_text")),
                        "outbound_route": _clean_text(outbound_leg.get("route_text")),
                        "return_time": _clean_text(inbound_leg.get("time_text")),
                        "return_route": _clean_text(inbound_leg.get("route_text")),
                        "deep_link": result.deep_link,
                    }
                )
            return preview

        log.info(
            "scrapingbee_multi_city_debug",
            outbound=f"{outbound_origin}->{outbound_destination}",
            inbound=f"{inbound_origin}->{inbound_destination}",
            outbound_date=outbound_date.isoformat(),
            inbound_date=inbound_date.isoformat(),
            max_stops=max_stops,
            used_deep_pass=used_deep_pass,
            target_url=target_url,
            summary_prices=summary_prices,
            card_count=card_count,
            captured_count=captured_count,
            raw_results_count=len(raw_results),
            eligible_results_count=len(eligible_results),
            raw_preview=_preview(raw_results),
            eligible_preview=_preview(eligible_results),
        )

    def _annotate_multi_city_results(
        self,
        results: list[ProviderResult],
        *,
        outbound_origin: str,
        outbound_destination: str,
        outbound_date: date,
        inbound_origin: str,
        inbound_destination: str,
        inbound_date: date,
    ) -> list[ProviderResult]:
        annotated: list[ProviderResult] = []
        for result in results:
            airline_parts = [part.strip() for part in result.airline.split("/") if part.strip()]
            outbound_airline = airline_parts[0] if airline_parts else result.airline
            return_airline = airline_parts[-1] if len(airline_parts) > 1 else ""
            result.raw_data.update(
                {
                    "trip_type": "multi_city",
                    "outbound": {
                        "origin": outbound_origin,
                        "destination": outbound_destination,
                        "date": outbound_date.isoformat(),
                    },
                    "inbound": {
                        "origin": inbound_origin,
                        "destination": inbound_destination,
                        "date": inbound_date.isoformat(),
                    },
                    "outbound_airline": outbound_airline,
                    "return_airline": return_airline,
                    "return_origin": inbound_origin,
                    "return_destination": inbound_destination,
                    "return_date": inbound_date.isoformat(),
                }
            )
            annotated.append(result)
        return annotated

    def _parse_price(self, value: object) -> float | None:
        text = _clean_text(value)
        match = _MONEY_RE.search(text.replace(" ", ""))
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None

    def _route_airport_pair(self, route_text: object) -> tuple[str, str] | None:
        """Pull the actual departure/arrival airport codes from a leg's route text.

        When a group is searched with a city/metro code (e.g. ROM = all Rome
        airports), Kayak returns a fare on a specific airport (e.g. FCO). Kayak
        renders that on the card as an airport pair like "FCO-IAD"; we surface it
        so logs/export show the airport actually flown, not the searched code.
        """
        text = _clean_text(route_text).upper()
        match = _AIRPORT_PAIR_RE.search(text)
        if not match:
            return None
        return match.group(1), match.group(2)

    def _parse_duration_minutes(
        self,
        summary: str,
        duration_text: str,
        duration_value: object = None,
    ) -> int:
        if isinstance(duration_value, (int, float)) and duration_value > 0:
            return int(duration_value)

        haystack = f"{summary} {duration_text}".strip()
        match = _HOURS_MINUTES_RE.search(haystack)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2) or 0)
            return hours * 60 + minutes

        match = _MINUTES_ONLY_RE.search(haystack)
        if match:
            return int(match.group(1))

        return 0

    def _parse_stops(self, summary: str, stops_value: object = None) -> int:
        if isinstance(stops_value, (int, float)) and stops_value >= 0:
            return int(stops_value)
        lowered = summary.lower()
        if "nonstop" in lowered or "non-stop" in lowered:
            return 0
        match = _STOPS_RE.search(summary)
        if match:
            return int(match.group(1))
        return 0

    def _looks_like_non_flight_transport(self, *texts: object) -> bool:
        haystack = " ".join(_clean_text(text).lower() for text in texts if _clean_text(text))
        if not haystack:
            return False
        return any(term in haystack for term in _NON_FLIGHT_TRANSPORT_TERMS)

    def _normalize_rendered_cards(
        self,
        payload: dict,
        *,
        currency: str,
        deep_link: str,
        trip_type: str,
        market_country_code: str,
        expected_leg_count: int,
    ) -> list[ProviderResult]:
        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list):
            return []

        results: list[ProviderResult] = []
        for card in raw_cards:
            if not isinstance(card, dict):
                continue

            price = self._parse_price(card.get("price_text")) or self._parse_price(card.get("text"))
            if price is None:
                continue
            initial_price = self._parse_price(card.get("initial_price_text"))

            legs = card.get("legs")
            if not isinstance(legs, list) or len(legs) < expected_leg_count:
                continue

            normalized_legs: list[dict[str, object]] = []
            unique_airlines: list[str] = []
            leg_stop_counts: list[int] = []
            leg_durations: list[int] = []
            total_duration = 0
            for leg in legs[:expected_leg_count]:
                if not isinstance(leg, dict):
                    continue
                airline = _clean_text(leg.get("airline"))
                if airline and airline not in unique_airlines:
                    unique_airlines.append(airline)
                duration_text = _clean_text(leg.get("duration_text"))
                layover_text = _clean_text(leg.get("layover_text"))
                stops_text = _clean_text(leg.get("stops_text"))
                route_text = _clean_text(leg.get("route_text"))
                leg_duration = self._parse_duration_minutes(
                    "",
                    duration_text,
                )
                leg_durations.append(leg_duration)
                total_duration += leg_duration
                leg_stops = self._parse_stops(f"{stops_text} {layover_text}".strip())
                leg_stop_counts.append(leg_stops)
                airport_pair = self._route_airport_pair(route_text)
                normalized_legs.append(
                    {
                        "airline": airline,
                        "time_text": _clean_text(leg.get("time_text")),
                        "route_text": route_text,
                        "actual_origin": airport_pair[0] if airport_pair else "",
                        "actual_destination": airport_pair[1] if airport_pair else "",
                        "stops_text": stops_text,
                        "layover_text": layover_text,
                        "duration_text": duration_text,
                        "duration_minutes": leg_duration,
                        "text": _clean_text(leg.get("text")),
                    }
                )

            if len(normalized_legs) < expected_leg_count:
                continue

            total_stops = sum(leg_stop_counts)

            airline_text = _clean_text(card.get("airline_text"))
            airline_parts = [part.strip() for part in airline_text.split("/") if part.strip()]
            display_airline = airline_text or " / ".join(unique_airlines) or "Unknown airline"
            booking_href = _clean_text(card.get("booking_href"))
            # The card's /book/ href is a session-specific redirect that EXPIRES,
            # so it is useless for later verification. Save the stable Kayak search
            # URL instead (re-runs the same search); keep the booking href in
            # raw_data only for reference.
            normalized_link = deep_link
            card_text = _clean_text(card.get("text"))
            actual_currency = self._detect_display_currency(
                card.get("price_text") or card_text,
                requested_currency=currency,
                market_country_code=market_country_code,
            )
            if self._looks_like_non_flight_transport(
                card_text,
                card.get("airline_text"),
                *(
                    " ".join(
                        [
                            _clean_text(leg.get("text")),
                            _clean_text(leg.get("route_text")),
                            _clean_text(leg.get("layover_text")),
                        ]
                    )
                    for leg in normalized_legs
                ),
            ):
                continue
            badges = [
                _clean_text(badge)
                for badge in (card.get("badges") or [])
                if _clean_text(badge)
            ]

            results.append(
                ProviderResult(
                    price=price,
                    currency=actual_currency,
                    airline=display_airline,
                    deep_link=normalized_link,
                    provider=self.name,
                    duration_minutes=total_duration,
                    stops=total_stops,
                    raw_data={
                        "trip_type": trip_type,
                        "duration_text": " / ".join(
                            leg["duration_text"]
                            for leg in normalized_legs
                            if isinstance(leg.get("duration_text"), str) and leg["duration_text"]
                        ),
                        "price_text": _clean_text(card.get("price_text")),
                        "initial_price_text": _clean_text(card.get("initial_price_text")),
                        "final_price_text": _clean_text(card.get("price_text")),
                        "initial_price": initial_price,
                        "final_price": price,
                        "price_adjusted_after_settle": (
                            initial_price is not None and initial_price != price
                        ),
                        "summary": card_text,
                        "cabin": _clean_text(card.get("cabin")),
                        "badges": badges,
                        "booking_href": booking_href,
                        "legs": normalized_legs,
                        "airline_names": unique_airlines or airline_parts,
                        "leg_stops": leg_stop_counts,
                        "leg_durations": leg_durations,
                        "stop_result_label": (
                            self._stop_label_from_leg_stops(leg_stop_counts)
                            if expected_leg_count > 1
                            else self._stop_label_for_count(total_stops)
                        ),
                    },
                )
            )

            raw_data = results[-1].raw_data
            # Actual airports flown (e.g. FCO when the group searched ROM), so
            # logs/export can show the real airport instead of the searched code.
            actual_outbound_origin = _clean_text(normalized_legs[0].get("actual_origin"))
            actual_outbound_destination = _clean_text(normalized_legs[0].get("actual_destination"))
            if actual_outbound_origin:
                raw_data["actual_outbound_origin"] = actual_outbound_origin
            if actual_outbound_destination:
                raw_data["actual_outbound_destination"] = actual_outbound_destination
            if len(normalized_legs) > 1:
                actual_return_origin = _clean_text(normalized_legs[-1].get("actual_origin"))
                actual_return_destination = _clean_text(normalized_legs[-1].get("actual_destination"))
                if actual_return_origin:
                    raw_data["actual_return_origin"] = actual_return_origin
                if actual_return_destination:
                    raw_data["actual_return_destination"] = actual_return_destination
            if trip_type in {"round_trip", "multi_city"}:
                outbound_airline = _clean_text(normalized_legs[0].get("airline")) or (
                    airline_parts[0] if airline_parts else display_airline
                )
                return_airline = outbound_airline
                if len(normalized_legs) > 1:
                    return_airline = _clean_text(normalized_legs[-1].get("airline")) or (
                        airline_parts[-1] if len(airline_parts) > 1 else outbound_airline
                    )
                raw_data["outbound_airline"] = outbound_airline
                raw_data["return_airline"] = return_airline

        return sorted(results, key=lambda item: item.price)

    def _normalize_flights(
        self,
        payload: dict,
        *,
        currency: str,
        deep_link: str,
        trip_type: str,
        market_country_code: str,
    ) -> list[ProviderResult]:
        offers = payload.get("offers")
        if not isinstance(offers, list):
            offers = payload.get("flights")
        if not isinstance(offers, list):
            return []

        results: list[ProviderResult] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue

            price = self._parse_price(offer.get("price"))
            if price is None:
                continue

            airline = _clean_text(offer.get("airline")) or "Unknown airline"
            duration_text = _clean_text(offer.get("duration_text")) or _clean_text(offer.get("time"))
            summary = _clean_text(offer.get("summary"))
            offer_link = _clean_text(offer.get("link"))
            normalized_link = urljoin(deep_link, offer_link) if offer_link else deep_link
            actual_currency = self._detect_display_currency(
                offer.get("price_text") or summary,
                requested_currency=currency,
                market_country_code=market_country_code,
            )
            if self._looks_like_non_flight_transport(
                airline,
                summary,
                duration_text,
            ):
                continue

            airline_parts = [part.strip() for part in airline.split("/") if part.strip()]
            stop_count = self._parse_stops(summary, offer.get("stops"))
            duration_minutes = self._parse_duration_minutes(
                summary,
                duration_text,
                offer.get("duration"),
            )
            raw_data = {
                "trip_type": trip_type,
                "price_text": _clean_text(offer.get("price_text")),
                "duration_text": duration_text,
                "summary": summary,
                "airline_names": airline_parts or ([airline] if airline else []),
                "leg_stops": [stop_count],
                "leg_durations": [duration_minutes],
                "stop_result_label": self._stop_label_for_count(stop_count),
            }
            if trip_type in {"round_trip", "multi_city"}:
                outbound_airline = airline_parts[0] if airline_parts else airline
                return_airline = airline_parts[1] if len(airline_parts) > 1 else outbound_airline
                raw_data["outbound_airline"] = outbound_airline
                raw_data["return_airline"] = return_airline

            results.append(
                ProviderResult(
                    price=price,
                    currency=actual_currency,
                    airline=airline,
                    deep_link=normalized_link,
                    provider=self.name,
                    duration_minutes=duration_minutes,
                    stops=stop_count,
                    raw_data=raw_data,
                )
            )

        return sorted(results, key=lambda item: item.price)

    async def _render_results_attempt(
        self,
        *,
        target_url: str,
        country_code: str,
        currency: str,
        trip_type: str,
        minimum_leg_count: int,
        deep: bool,
        same_airline_only: bool,
        max_stops: int | None = None,
        same_airline_wait_ms: int | None = None,
        budget: "_RenderBudget | None" = None,
    ) -> tuple[dict, dict[str, str], list[ProviderResult], int, int]:
        rendered = await self._get_rendered_payload(
            target_url,
            js_scenario=self._build_results_scenario(
                deep=deep,
                same_airline_only=same_airline_only,
                minimum_leg_count=minimum_leg_count,
                same_airline_wait_ms=same_airline_wait_ms,
                max_stops=max_stops,
            ),
            country_code=country_code,
        )
        # A render that returned (no exception) is a CHARGED render -- count it
        # against the per-search budget. (Failures raise above and never reach here,
        # so they stay free and uncounted.)
        if budget is not None:
            budget.consume()
        summary_prices = self._multi_city_summary_prices(rendered)
        results, card_count, captured_count = await self._parse_rendered_payload(
            rendered,
            currency=currency,
            deep_link=target_url,
            market_country_code=country_code,
            trip_type=trip_type,
            expected_leg_count=minimum_leg_count,
        )
        return rendered, summary_prices, results, card_count, captured_count

    async def _search_rendered_itinerary_diagnostic(
        self,
        *,
        trip_type: str,
        target_url: str,
        requested_market: str | None,
        requested_currency: str,
        market_country_code: str,
        max_stops: int | None,
        same_airline_only: bool,
        minimum_leg_count: int,
    ) -> ProviderSearchOutcome:
        same_airline_only = bool(same_airline_only)
        initial_deep = True
        used_hollow_retry = False
        # Per-search charged-render budget (caps credits/entry; see
        # _MAX_RENDERS_PER_SEARCH). The initial render below consumes 1.
        budget = _RenderBudget(_MAX_RENDERS_PER_SEARCH)
        rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=requested_currency,
            trip_type=trip_type,
            minimum_leg_count=minimum_leg_count,
            deep=initial_deep,
            same_airline_only=same_airline_only,
            max_stops=max_stops,
            budget=budget,
        )
        # Hollow-render retry: the session got NO inventory at all (no cards, no
        # 'no results' page, empty poll JSON) -- a dud proxy/browser session, not a
        # statement about the date. One plain retry (which lands on a fresh proxy
        # IP) is the proven fix; without it the dud either errors out or, worse,
        # the -MULT fallback below re-renders a hopeless session with looser
        # filters.
        if budget.has_room() and self._looks_hollow_render(rendered, results, card_count):
            used_hollow_retry = True
            log.warning(
                "scrapingbee_hollow_render_retry",
                trip_type=trip_type,
                target_url=target_url,
            )
            rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
                target_url=target_url,
                country_code=market_country_code,
                currency=requested_currency,
                trip_type=trip_type,
                minimum_leg_count=minimum_leg_count,
                deep=initial_deep,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                budget=budget,
            )
        # 0-card fallback: if the -MULT URL rendered ZERO cards (and Kayak didn't
        # explicitly say "no results"), re-render WITHOUT -MULT; the Python
        # same-airline filter still isolates carriers. NOTE: the founding
        # incident ("the historical YEG-KEF glitch") could NOT be reproduced on
        # 2026-06-10 (3x -MULT renders: 1 generic dud + 2 perfect pages) -- it
        # likely predates the flylocal token or was a dud render misdiagnosed.
        # Kept as cheap insurance: it can only fire in a narrow state (rendered
        # page, zero cards, no no-results message, not hollow) and costs at most
        # one render.
        # Skipped when the hollow retry ALSO came back hollow -- a second dud
        # session means the problem is the session/proxy, not the -MULT filter,
        # and a third render this attempt would be credit burn (the date retries
        # next cycle under the error caps instead).
        if (
            budget.has_room()
            and "airlines=-MULT" in target_url
            and card_count == 0
            and not results
            and not self._rendered_payload_reports_no_results(rendered)
            and not (
                used_hollow_retry
                and self._looks_hollow_render(rendered, results, card_count)
            )
        ):
            # Drop the airlines=-MULT,flylocal token from the fs= clause; if that
            # leaves fs= empty, remove fs= entirely. Leaves any stops= clause intact.
            fallback_url = re.sub(r"airlines=-MULT,flylocal;?", "", target_url)
            fallback_url = re.sub(r"[?&]fs=(?=&|$)", "", fallback_url)
            log.info(
                "scrapingbee_mult_zero_cards_fallback",
                trip_type=trip_type,
                target_url=target_url,
                fallback_url=fallback_url,
            )
            rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
                target_url=fallback_url,
                country_code=market_country_code,
                currency=requested_currency,
                trip_type=trip_type,
                minimum_leg_count=minimum_leg_count,
                deep=initial_deep,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                budget=budget,
            )
        eligible_results = self._eligible_same_airline_results(
            results, max_stops, same_airline_only
        )
        raw_offers_found = len(results)
        used_strong_retry = False
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        # NOTE: the old "_try_alternate_airline_facets" retry that used to live here
        # was DELETED: its airline_facet_index never reached the scenario (the
        # builder discards it), so it re-rendered the IDENTICAL URL up to 2 extra
        # times on sparse/high-price dates -- pure credit burn with zero effect.

        if (
            budget.has_room()
            and not eligible_results
            and not results
            and card_count == 0
            and not self._rendered_payload_has_summary_prices(rendered)
            and self._strong_retry_worthwhile(rendered)
        ):
            retry_rendered, retry_summary_prices, retry_results, retry_card_count, retry_captured_count = await self._render_results_attempt(
                target_url=target_url,
                country_code=market_country_code,
                currency=requested_currency,
                trip_type=trip_type,
                minimum_leg_count=minimum_leg_count,
                deep=True,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                same_airline_wait_ms=_SAME_AIRLINE_RETRY_WAIT_MS,
                budget=budget,
            )
            raw_offers_found = max(raw_offers_found, len(retry_results))
            if retry_results or retry_card_count > 0 or self._rendered_payload_has_summary_prices(retry_rendered):
                rendered = retry_rendered
                summary_prices = retry_summary_prices
                results = retry_results
                card_count = retry_card_count
                captured_count = retry_captured_count
                eligible_results = self._eligible_same_airline_results(
                    retry_results, max_stops, same_airline_only
                )
                used_strong_retry = True
                selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
            else:
                eligible_results = []

        (
            rendered,
            summary_prices,
            results,
            card_count,
            captured_count,
            eligible_results,
            poll_refused,
            poll_retried,
        ) = await self._enforce_two_witness_agreement(
            target_url=target_url,
            country_code=market_country_code,
            currency=requested_currency,
            trip_type=trip_type,
            minimum_leg_count=minimum_leg_count,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            rendered=rendered,
            summary_prices=summary_prices,
            results=results,
            card_count=card_count,
            captured_count=captured_count,
            eligible_results=eligible_results,
            retry_allowed=not (used_hollow_retry or used_strong_retry) and budget.has_room(),
            budget=budget,
        )
        used_strong_retry = used_strong_retry or poll_retried
        if poll_refused:
            eligible_results = []

        if not results and card_count == 0 and not self._rendered_payload_has_summary_prices(rendered):
            log.warning(
                "scrapingbee_render_no_cards",
                trip_type=trip_type,
                target_url=target_url,
                no_results=self._rendered_payload_reports_no_results(rendered),
                **self._render_failure_snapshot(rendered),
            )
            raise ValueError("KAYAK rendered page did not expose extractable result cards.")

        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        visible_results_found = card_count > 0 or facet_option_count > 0
        summary_price_found = bool(summary_prices) or facet_option_count > 0

        if eligible_results:
            result_reason = "success"
        elif poll_refused:
            # The witnesses disagreed twice: not a data statement about the
            # date, a render we refuse to trust. extract_failed retries under
            # the error caps instead of parking as no-fare.
            result_reason = "extract_failed"
        elif results or visible_results_found:
            result_reason = "filtered_out"
        elif summary_price_found:
            result_reason = "extract_failed"
        else:
            result_reason = "page_empty"

        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=requested_market,
            requested_currency=requested_currency,
            result_reason=result_reason,
            visible_results_found=visible_results_found,
            summary_price_found=summary_price_found,
            used_strong_retry=used_strong_retry or bool(selected_facet),
        )
        diagnostics.raw_offers_found = raw_offers_found
        diagnostics.eligible_offers_found = len(eligible_results)
        log.info(
            "scrapingbee_results",
            trip_type=trip_type,
            target_url=target_url,
            result_reason=result_reason,
            raw_offers_found=raw_offers_found,
            eligible_offers_found=len(eligible_results),
            card_count=card_count,
            captured_count=captured_count,
            selected_facet=selected_facet,
            facet_option_count=facet_option_count,
            visible_results_found=visible_results_found,
            summary_price_found=summary_price_found,
            used_strong_retry=used_strong_retry,
            hollow_retry=used_hollow_retry,
            poll_refused=poll_refused,
            actual_route=self._actual_route_label(eligible_results),
            **self._accuracy_audit(
                rendered=rendered,
                summary_prices=summary_prices,
                eligible_results=eligible_results,
            ),
            **self._render_quality_fields(
                rendered=rendered,
                eligible_results=eligible_results,
                max_stops=max_stops,
                same_airline_only=same_airline_only,
            ),
        )
        return ProviderSearchOutcome(results=eligible_results, diagnostics=diagnostics)

    async def _search_multi_city_once(
        self,
        legs: list[dict[str, object]],
        *,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> tuple[list[ProviderResult], ProviderSearchDiagnostics]:
        same_airline_only = bool(same_airline_only)
        if self._quota_blocked():
            raise ProviderQuotaExhaustedError("ScrapingBee quota cooldown active.")

        # 2-4 legs supported (3/4-leg open-jaw chains probed live 2026-06-10).
        if not 2 <= len(legs) <= 4:
            raise ValueError("multi_city search requires 2 to 4 legs.")
        for leg in legs:
            if not isinstance(leg.get("outbound_date"), date):
                raise ValueError("every multi_city leg needs an outbound_date.")

        leg_count = len(legs)
        outbound = legs[0]
        inbound = legs[-1]
        outbound_date = outbound["outbound_date"]
        inbound_date = inbound["outbound_date"]
        outbound_origin = str(outbound["departure_id"]).upper()
        outbound_destination = str(outbound["arrival_id"]).upper()
        inbound_origin = str(inbound["departure_id"]).upper()
        inbound_destination = str(inbound["arrival_id"]).upper()
        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_multi_city_chain_url(
            legs=legs,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline=same_airline_only,
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )

        used_deep_pass = True
        rendered: dict = {}
        summary_prices: dict[str, str] = {}
        results: list[ProviderResult] = []
        card_count = 0
        captured_count = 0
        used_hollow_retry = False
        # Per-search charged-render budget (caps credits/entry; see
        # _MAX_RENDERS_PER_SEARCH). Shared across this date's retry layers so they
        # cannot stack -- once the cap is hit, every further retry is skipped.
        budget = _RenderBudget(_MAX_RENDERS_PER_SEARCH)

        rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=currency,
            trip_type="multi_city",
            minimum_leg_count=leg_count,
            deep=used_deep_pass,
            same_airline_only=same_airline_only,
            max_stops=max_stops,
            budget=budget,
        )
        # Hollow-render retry (see _search_rendered_itinerary_diagnostic): a dud
        # session saw no inventory at all; retry ONCE (fresh proxy IP).
        if budget.has_room() and self._looks_hollow_render(rendered, results, card_count):
            used_hollow_retry = True
            log.warning(
                "scrapingbee_hollow_render_retry",
                trip_type="multi_city",
                target_url=target_url,
            )
            rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
                target_url=target_url,
                country_code=market_country_code,
                currency=currency,
                trip_type="multi_city",
                minimum_leg_count=leg_count,
                deep=used_deep_pass,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                budget=budget,
            )
        eligible_results = self._eligible_same_airline_results(
            results, max_stops, same_airline_only
        )
        raw_offers_found = len(results)
        used_strong_retry = False
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        # The dead "_try_alternate_airline_facets" retry was DELETED here too --
        # see _search_rendered_itinerary_diagnostic for the rationale.

        if (
            budget.has_room()
            and not eligible_results
            and not results
            and card_count == 0
            and not self._rendered_payload_has_summary_prices(rendered)
            and self._strong_retry_worthwhile(rendered)
        ):
            retry_rendered, retry_summary_prices, retry_results, retry_card_count, retry_captured_count = await self._render_results_attempt(
                target_url=target_url,
                country_code=market_country_code,
                currency=currency,
                trip_type="multi_city",
                minimum_leg_count=leg_count,
                deep=True,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                same_airline_wait_ms=_SAME_AIRLINE_RETRY_WAIT_MS,
                budget=budget,
            )
            raw_offers_found = max(raw_offers_found, len(retry_results))
            if (
                retry_results
                or retry_card_count > 0
                or self._rendered_payload_has_summary_prices(retry_rendered)
            ):
                rendered = retry_rendered
                summary_prices = retry_summary_prices
                results = retry_results
                card_count = retry_card_count
                captured_count = retry_captured_count
                eligible_results = self._eligible_same_airline_results(
                    retry_results, max_stops, same_airline_only
                )
                used_strong_retry = True
                selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
            else:
                eligible_results = []

        (
            rendered,
            summary_prices,
            results,
            card_count,
            captured_count,
            eligible_results,
            poll_refused,
            poll_retried,
        ) = await self._enforce_two_witness_agreement(
            target_url=target_url,
            country_code=market_country_code,
            currency=currency,
            trip_type="multi_city",
            minimum_leg_count=leg_count,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            rendered=rendered,
            summary_prices=summary_prices,
            results=results,
            card_count=card_count,
            captured_count=captured_count,
            eligible_results=eligible_results,
            retry_allowed=not (used_hollow_retry or used_strong_retry) and budget.has_room(),
            budget=budget,
        )
        used_strong_retry = used_strong_retry or poll_retried
        if poll_refused:
            eligible_results = []

        if not results and card_count == 0 and not self._rendered_payload_has_summary_prices(rendered):
            log.warning(
                "scrapingbee_render_no_cards",
                trip_type="multi_city",
                target_url=target_url,
                no_results=self._rendered_payload_reports_no_results(rendered),
                **self._render_failure_snapshot(rendered),
            )
            raise ValueError("KAYAK rendered page did not expose extractable result cards.")
        self._log_multi_city_debug_snapshot(
            outbound_origin=outbound_origin,
            outbound_destination=outbound_destination,
            outbound_date=outbound_date,
            inbound_origin=inbound_origin,
            inbound_destination=inbound_destination,
            inbound_date=inbound_date,
            target_url=target_url,
            summary_prices=summary_prices,
            card_count=card_count,
            captured_count=captured_count,
            raw_results=results,
            eligible_results=eligible_results,
            max_stops=max_stops,
            used_deep_pass=used_deep_pass,
        )
        eligible_results = self._annotate_multi_city_results(
            eligible_results,
            outbound_origin=outbound_origin,
            outbound_destination=outbound_destination,
            outbound_date=outbound_date,
            inbound_origin=inbound_origin,
            inbound_destination=inbound_destination,
            inbound_date=inbound_date,
        )
        log.info(
            "scrapingbee_results",
            trip_type="multi_city",
            outbound=f"{outbound_origin}->{outbound_destination}",
            inbound=f"{inbound_origin}->{inbound_destination}",
            count=len(eligible_results),
            currency=currency,
            target_url=target_url,
            hollow_retry=used_hollow_retry,
            poll_refused=poll_refused,
            actual_route=self._actual_route_label(eligible_results),
            **self._accuracy_audit(
                rendered=rendered,
                summary_prices=summary_prices,
                eligible_results=eligible_results,
            ),
            **self._render_quality_fields(
                rendered=rendered,
                eligible_results=eligible_results,
                max_stops=max_stops,
                same_airline_only=same_airline_only,
            ),
        )
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        visible_results_found = card_count > 0 or facet_option_count > 0
        summary_price_found = bool(summary_prices) or facet_option_count > 0
        if eligible_results:
            result_reason = "success"
        elif poll_refused:
            result_reason = "extract_failed"  # witnesses disagreed twice -> retryable
        elif results:
            result_reason = "filtered_out"
        elif visible_results_found or summary_price_found:
            result_reason = "extract_failed"
        else:
            result_reason = "page_empty"

        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=market,
            requested_currency=currency,
            result_reason=result_reason,
            visible_results_found=visible_results_found,
            summary_price_found=summary_price_found,
            used_strong_retry=used_strong_retry or bool(selected_facet),
        )
        diagnostics.raw_offers_found = raw_offers_found
        diagnostics.eligible_offers_found = len(eligible_results)
        return eligible_results, diagnostics

    async def search_round_trip_diagnostic(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        same_airline_only = bool(same_airline_only)
        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_search_url(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_url=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        return await self._search_rendered_itinerary_diagnostic(
            trip_type="round_trip",
            target_url=target_url,
            requested_market=market,
            requested_currency=currency,
            market_country_code=market_country_code,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            minimum_leg_count=2,
        )

    async def search_multi_city_diagnostic(
        self,
        *,
        legs: list[dict[str, object]],
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
        max_leg_duration_minutes: int | None = None,
        max_layover_minutes: int | None = None,
    ) -> ProviderSearchOutcome:
        results, diagnostics = await self._search_multi_city_once(
            legs=legs,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=bool(same_airline_only),
            max_leg_duration_minutes=max_leg_duration_minutes,
            max_layover_minutes=max_layover_minutes,
        )
        return ProviderSearchOutcome(results=results, diagnostics=diagnostics)

    def _should_retry(self, exc: BaseException) -> bool:
        return isinstance(exc, RuntimeError) and not isinstance(
            exc,
            (
                ProviderQuotaExhaustedError,
                ProviderAuthError,
            ),
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
        del adults, cabin
        same_airline_only = bool(same_airline_only)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=2, max=12),
            retry=retry_if_exception_type(RuntimeError)
            & retry_if_exception(self._should_retry),
            reraise=True,
        ):
            with attempt:
                outcome = await self.search_round_trip_diagnostic(
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    return_date=return_date,
                    market=market,
                    currency=currency,
                    max_stops=max_stops,
                    same_airline_only=same_airline_only,
                    max_leg_duration_minutes=max_leg_duration_minutes,
                    max_layover_minutes=max_layover_minutes,
                )
                return self._filter_results_by_stops(outcome.results, max_stops)

        return []

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
        del adults, cabin
        same_airline_only = bool(same_airline_only)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=2, max=12),
            retry=retry_if_exception_type(RuntimeError)
            & retry_if_exception(self._should_retry),
            reraise=True,
        ):
            with attempt:
                outcome = await self.search_multi_city_diagnostic(
                    legs=legs,
                    market=market,
                    currency=currency,
                    max_stops=max_stops,
                    same_airline_only=same_airline_only,
                    max_leg_duration_minutes=max_leg_duration_minutes,
                    max_layover_minutes=max_layover_minutes,
                )
                results = outcome.results
                if max_stops is None:
                    return results
                return self._filter_results_by_stops(results, max_stops)

        return []


class ScrapingBeePoolProvider:
    name = "scrapingbee"

    def __init__(
        self,
        api_keys: list[str],
        **provider_kwargs: object,
    ) -> None:
        self._providers = [
            ScrapingBeeProvider(api_key=api_key, **provider_kwargs)
            for api_key in api_keys
            if api_key.strip()
        ]
        self._cursor = 0

    def is_configured(self) -> bool:
        return any(provider.is_configured() for provider in self._providers)

    def _ordered_providers(self) -> list[ScrapingBeeProvider]:
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
                RuntimeError,
            ) as exc:
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc

        return ProviderSearchOutcome(results=[])

    async def _search_with_failover(self, search_fn) -> list[ProviderResult]:
        outcome = await self._search_outcome_with_failover(search_fn)
        return outcome.results

    async def search_round_trip_diagnostic(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date,
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
        return await self._search_with_failover(
            lambda provider: provider.search_round_trip(
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
        return await self._search_with_failover(
            lambda provider: provider.search_multi_city(
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

    async def close(self) -> None:
        for provider in self._providers:
            await provider.close()
