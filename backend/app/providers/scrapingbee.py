from __future__ import annotations

import asyncio
import json
import re
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
    ProviderSearchDiagnostics,
    ProviderSearchOutcome,
    ProviderResult,
)

log = get_logger(__name__)

_BASE_URL = "https://app.scrapingbee.com/api/v1"
_KAYAK_DEFAULT_HOST = "www.kayak.com"
_MONEY_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")
_HOURS_MINUTES_RE = re.compile(r"(?i)(\d+)\s*(?:hours|hour|hrs|hr|h)\s*(?:(\d+)\s*(?:minutes|minute|mins|min|m))?")
_MINUTES_ONLY_RE = re.compile(r"(?i)(\d+)\s*(?:minutes|minute|mins|min|m)")
_STOPS_RE = re.compile(r"(?i)\b(\d+)\s+stop(?:s)?\b")
_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
_FLIGHT_COUNT_RE = re.compile(r"(?i)\b(?:(\d+)\s+of\s+)?(\d+)\s+flights\b")
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
_FAST_MULTI_CITY_CARD_LIMIT = 30
_DEEP_MULTI_CITY_CARD_LIMIT = 180
_DEEP_RESULTS_JS_SCENARIO = {
    "instructions": [
        {"wait": 5000},
        {"evaluate": "window.scrollTo(0, document.body.scrollHeight * 0.35);"},
        {"wait": 2500},
        {"evaluate": "window.scrollTo(0, document.body.scrollHeight * 0.7);"},
        {"wait": 2500},
        {"evaluate": "window.scrollTo(0, document.body.scrollHeight);"},
        {"wait": 3000},
    ]
}


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

    _AI_EXTRACT_RULES = {
        "offers": {
            "description": "visible KAYAK flight offers on the page",
            "type": "list",
            "output": {
                "price": {
                    "description": "total itinerary price as a number without currency symbols",
                    "type": "number",
                },
                "price_text": {
                    "description": "exact displayed itinerary price text including currency symbol or code",
                    "type": "string",
                },
                "airline": {
                    "description": "airline name or airline combination shown for the itinerary",
                    "type": "string",
                },
                "duration": {
                    "description": "total itinerary duration in minutes if inferable, otherwise 0",
                    "type": "number",
                },
                "duration_text": {
                    "description": "displayed itinerary duration text such as 23h 10m",
                    "type": "string",
                },
                "stops": {
                    "description": "number of stops as an integer where nonstop is 0",
                    "type": "number",
                },
                "link": {
                    "description": "deal or booking link for the itinerary if visible",
                    "type": "string",
                },
                "summary": {
                    "description": "short itinerary summary including times, stops, and other visible details",
                    "type": "string",
                },
            },
        }
    }
    _MULTI_CITY_AI_EXTRACT_RULES = {
        "offers": {
            "description": "visible KAYAK multi-city flight offers on the page",
            "type": "list",
            "output": {
                "price": {
                    "description": "total itinerary price as a number without currency symbols",
                    "type": "number",
                },
                "price_text": {
                    "description": "exact displayed itinerary price text including currency symbol or code",
                    "type": "string",
                },
                "airline": {
                    "description": "overall airline label shown for the itinerary",
                    "type": "string",
                },
                "outbound_airline": {
                    "description": "airline shown for the outbound leg",
                    "type": "string",
                },
                "return_airline": {
                    "description": "airline shown for the return leg",
                    "type": "string",
                },
                "outbound_duration_text": {
                    "description": "displayed outbound leg duration such as 13h 40m",
                    "type": "string",
                },
                "return_duration_text": {
                    "description": "displayed return leg duration such as 10h 19m",
                    "type": "string",
                },
                "outbound_stops": {
                    "description": "number of stops for the outbound leg as an integer where direct is 0",
                    "type": "number",
                },
                "return_stops": {
                    "description": "number of stops for the return leg as an integer where direct is 0",
                    "type": "number",
                },
                "outbound_stops_text": {
                    "description": "displayed outbound stops text such as 1 stop or 2 stops",
                    "type": "string",
                },
                "return_stops_text": {
                    "description": "displayed return stops text such as 1 stop or 2 stops",
                    "type": "string",
                },
                "outbound_time_text": {
                    "description": "displayed outbound time range text",
                    "type": "string",
                },
                "return_time_text": {
                    "description": "displayed return time range text",
                    "type": "string",
                },
                "outbound_route_text": {
                    "description": "displayed outbound route text with airports or cities",
                    "type": "string",
                },
                "return_route_text": {
                    "description": "displayed return route text with airports or cities",
                    "type": "string",
                },
                "cabin": {
                    "description": "displayed cabin or fare family text",
                    "type": "string",
                },
                "link": {
                    "description": "deal or booking link for the itinerary if visible",
                    "type": "string",
                },
                "summary": {
                    "description": "short itinerary summary including both legs and visible details",
                    "type": "string",
                },
            },
        }
    }

    _JS_SCENARIO = {
        "instructions": [
            {"wait": 3000},
            {"evaluate": "window.scrollTo(0, document.body.scrollHeight);"},
            {"wait": 3000},
        ]
    }

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        timeout: int = 30,
        max_retries: int = 3,
        concurrency_limit: int = 2,
        min_delay_seconds: float = 1.0,
        quota_cooldown_seconds: int = 3600,
        country_code: str = "us",
        premium_proxy: bool = False,
        stealth_proxy: bool = False,
        multi_city_debug: bool = False,
        user_agent: str = "flight-harvester/1.0",
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._country_code = country_code.strip().lower()
        self._premium_proxy = premium_proxy
        self._stealth_proxy = stealth_proxy
        self._multi_city_debug = multi_city_debug
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent.strip() or "flight-harvester/1.0",
            },
        )

        self._semaphore = asyncio.Semaphore(max(1, concurrency_limit))
        self._throttle_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._min_delay_seconds = max(0.0, min_delay_seconds)
        self._quota_blocked_until = 0.0
        self._quota_cooldown_seconds = quota_cooldown_seconds
        self._last_multi_city_capture = self._empty_multi_city_capture_state()

    @staticmethod
    def _empty_multi_city_capture_state() -> dict[str, object]:
        return {
            "summary_prices": {},
            "card_count": 0,
            "captured_count": 0,
            "captured_sorts": [],
            "capture_incomplete": False,
            "count_text": "",
        }

    def is_configured(self) -> bool:
        return bool(self._api_key and self._base_url)

    async def close(self) -> None:
        await self._client.aclose()

    def _parse_partial_payload(self, body: str) -> dict | None:
        if '"offers"' not in body:
            return None

        offers_key = body.find('"offers"')
        list_start = body.find("[", offers_key)
        if list_start < 0:
            return None

        decoder = json.JSONDecoder()
        cursor = list_start + 1
        offers: list[dict[str, object]] = []

        while cursor < len(body):
            while cursor < len(body) and body[cursor] in " \r\n\t,":
                cursor += 1

            if cursor >= len(body) or body[cursor] == "]":
                break

            try:
                parsed, next_cursor = decoder.raw_decode(body, cursor)
            except json.JSONDecodeError:
                break

            if isinstance(parsed, dict):
                offers.append(parsed)
            cursor = next_cursor

        if not offers:
            return None

        return {"offers": offers}

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
            "timeout": min(self._timeout * 1000, 140_000),
            "wait": 4000,
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

    def _base_params(
        self,
        target_url: str,
        *,
        ai_extract_rules: dict[str, object] | None = None,
        js_scenario: dict[str, object] | None = None,
        country_code: str | None = None,
    ) -> dict[str, object]:
        params = self._base_request_params(target_url, country_code=country_code)
        params["ai_extract_rules"] = json.dumps(
            ai_extract_rules or self._AI_EXTRACT_RULES,
            separators=(",", ":"),
        )
        params["js_scenario"] = json.dumps(
            js_scenario or self._JS_SCENARIO,
            separators=(",", ":"),
        )
        return params

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

    async def _get_payload(
        self,
        target_url: str,
        *,
        ai_extract_rules: dict[str, object] | None = None,
        js_scenario: dict[str, object] | None = None,
        country_code: str | None = None,
    ) -> dict:
        async with self._semaphore:
            await self._wait_for_slot()
            try:
                response = await self._client.get(
                    self._base_url,
                    params=self._base_params(
                        target_url,
                        ai_extract_rules=ai_extract_rules,
                        js_scenario=js_scenario,
                        country_code=country_code,
                    ),
                )
            except httpx.TimeoutException as exc:
                raise RuntimeError("ScrapingBee request timed out.") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError("ScrapingBee request failed.") from exc

        self._raise_for_status(response)

        try:
            data = await asyncio.to_thread(response.json)
        except Exception as exc:
            partial = self._parse_partial_payload(response.text)
            if partial is not None:
                return partial
            raise RuntimeError("ScrapingBee returned invalid JSON.") from exc

        if not isinstance(data, dict):
            raise RuntimeError("ScrapingBee returned an unexpected response body.")

        return data

    async def _get_rendered_payload(
        self,
        target_url: str,
        *,
        js_scenario: dict[str, object],
        country_code: str | None = None,
        block_resources: bool = True,
        wait_ms: int = 2500,
    ) -> dict:
        params = self._base_request_params(target_url, country_code=country_code)
        params["json_response"] = "True"
        params["js_scenario"] = json.dumps(js_scenario, separators=(",", ":"))
        if block_resources:
            params["block_resources"] = "True"
        params["wait"] = wait_ms

        async with self._semaphore:
            await self._wait_for_slot()
            try:
                response = await self._client.get(
                    self._base_url,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                raise RuntimeError("ScrapingBee request timed out.") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError("ScrapingBee request failed.") from exc

        self._raise_for_status(response)

        try:
            data = await asyncio.to_thread(response.json)
        except Exception as exc:
            raise RuntimeError("ScrapingBee returned invalid rendered JSON.") from exc

        if not isinstance(data, dict):
            raise RuntimeError("ScrapingBee returned an unexpected rendered response body.")

        return data

    def _build_search_url(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date | None = None,
        market: str | None = None,
        currency: str = "USD",
    ) -> str:
        route = f"{origin.upper()}-{destination.upper()}"
        base_url = self._kayak_site_base(currency, market)
        if return_date:
            return (
                f"{base_url}/flights/{route}/"
                f"{depart_date.isoformat()}/{return_date.isoformat()}?sort=price_a"
            )
        return (
            f"{base_url}/flights/{route}/"
            f"{depart_date.isoformat()}?sort=price_a"
        )

    def _build_multi_city_js_scenario(
        self,
        legs: list[dict[str, object]],
    ) -> dict[str, object]:
        first = legs[0]
        second = legs[1]
        first_origin = str(first["departure_id"]).upper()
        first_destination = str(first["arrival_id"]).upper()
        second_origin = str(second["departure_id"]).upper()
        second_destination = str(second["arrival_id"]).upper()
        first_date = f"{first['outbound_date']:%B} {first['outbound_date'].day}, {first['outbound_date']:%Y}"
        second_date = f"{second['outbound_date']:%B} {second['outbound_date'].day}, {second['outbound_date']:%Y}"

        helper = (
            "window.fh={"
            "r:()=>{Array.from(document.querySelectorAll('[aria-label]'))"
            ".filter(e=>(e.getAttribute('aria-label')||'').includes('Remove leg number 3 from your search'))"
            ".forEach(n=>{let k=Object.keys(n).find(x=>x.startsWith('__reactProps$')),p=k&&n[k];"
            "p&&p.onClick?p.onClick({currentTarget:n,target:n,preventDefault(){},stopPropagation(){}}):n.click()});return 1},"
            "c:(s,i,v)=>{let n=document.querySelectorAll(s)[i],k=n&&Object.keys(n).find(x=>x.startsWith('__reactProps$')),p=k&&n[k];"
            "if(!p||!p.onChange)return 0;"
            "p.onChange({target:{value:v},currentTarget:n,preventDefault(){},stopPropagation(){}});return 1},"
            "o:(s,i)=>{let n=document.querySelectorAll(s)[i],l=n&&document.getElementById(n.getAttribute('aria-controls'));"
            "if(!l)return 0;"
            "let m=Array.from(l.querySelectorAll('[role=option]')).find(e=>e.id!=='nearby');"
            "if(!m)return 0;"
            "let k=Object.keys(m).find(x=>x.startsWith('__reactProps$')),p=k&&m[k];"
            "p&&p.onClick?p.onClick({currentTarget:m,target:m,preventDefault(){},stopPropagation(){}}):m.click();return 1},"
            "d:i=>{let n=Array.from(document.querySelectorAll('[aria-label]'))"
            ".filter(e=>(e.getAttribute('aria-label')||'').includes('Select start date from calendar input'))[i],"
            "k=n&&Object.keys(n).find(x=>x.startsWith('__reactProps$')),p=k&&n[k];"
            "if(!p||!p.onClick)return 0;"
            "p.onClick({currentTarget:n,target:n,preventDefault(){},stopPropagation(){}});return 1},"
            "p:l=>{let n=Array.from(document.querySelectorAll('[aria-label]')).find(e=>e.getAttribute('aria-label')===l),"
            "k=n&&Object.keys(n).find(x=>x.startsWith('__reactProps$')),p=k&&n[k];"
            "if(!n)return 0;"
            "p&&p.onClick?p.onClick({currentTarget:n,target:n,preventDefault(){},stopPropagation(){}}):n.click();return 1},"
            "s:()=>{let b=document.querySelector('button[aria-label=\"Search\"]'),k=b&&Object.keys(b).find(x=>x.startsWith('__reactProps$')),p=k&&b[k];"
            "if(!p||!p.onClick)return 0;"
            "p.onClick({currentTarget:b,target:b,preventDefault(){},stopPropagation(){}});return 1}"
            "};1"
        )

        return {
            "strict": False,
            "instructions": [
                {"evaluate": helper},
                {"evaluate": "fh.r()"},
                {"wait": 700},
                {"evaluate": f'fh.c("input[data-test-origin]",0,"{first_origin}")'},
                {"evaluate": f'fh.c("input[data-test-destination]",0,"{first_destination}")'},
                {"evaluate": f'fh.c("input[data-test-origin]",1,"{second_origin}")'},
                {"evaluate": f'fh.c("input[data-test-destination]",1,"{second_destination}")'},
                {"wait": 1500},
                {"evaluate": 'fh.o("input[data-test-origin]",0)'},
                {"evaluate": 'fh.o("input[data-test-destination]",0)'},
                {"evaluate": 'fh.o("input[data-test-origin]",1)'},
                {"evaluate": 'fh.o("input[data-test-destination]",1)'},
                {"wait": 1200},
                {"evaluate": "fh.d(0)"},
                {"wait": 700},
                {"evaluate": f'fh.p("{first_date}")'},
                {"wait": 700},
                {"evaluate": "fh.d(1)"},
                {"wait": 700},
                {"evaluate": f'fh.p("{second_date}")'},
                {"wait": 1200},
                {"evaluate": "fh.s()"},
                {"wait": 12_000},
            ],
        }

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
    ) -> str:
        base_url = self._kayak_site_base(currency, market)
        return (
            f"{base_url}/flights/"
            f"{outbound_origin.upper()}-{outbound_destination.upper()}/"
            f"{outbound_date.isoformat()}/"
            f"{inbound_origin.upper()}-{inbound_destination.upper()}/"
            f"{inbound_date.isoformat()}?sort=price_a"
        )

    def _build_multi_city_results_scenario(self, *, deep: bool = False) -> dict[str, object]:
        card_limit = _DEEP_MULTI_CITY_CARD_LIMIT if deep else _FAST_MULTI_CITY_CARD_LIMIT
        helper_script = (
            "(()=>{"
            "const fh=window.__fhResults||(window.__fhResults={});"
            "fh.clean=v=>(v||'').toString().replace(/\\s+/g,' ').trim();"
            "fh.visible=el=>{if(!el)return false;const r=el.getBoundingClientRect();"
            "return r.width>0&&r.height>0&&window.getComputedStyle(el).visibility!=='hidden';};"
            "fh.findSort=label=>Array.from(document.querySelectorAll('button,a,[role=\"button\"],div,span'))"
            ".filter(el=>fh.visible(el)).find(el=>new RegExp('^'+label+'(?:\\\\s|$)').test(fh.clean(el.innerText||el.getAttribute('aria-label')).toLowerCase()));"
            "fh.clickSort=label=>{const pick=fh.findSort(label);"
            "if(!pick)return false;"
            "const target=pick.closest('button,a,[role=\"button\"]')||pick;"
            "target.click();"
            "return true;};"
            "fh.countText=()=>{"
            "const countNode=Array.from(document.querySelectorAll('body *')).find(el=>{"
            "if(!fh.visible(el))return false;"
            "const text=fh.clean(el.innerText);"
            "return /\\b\\d+\\s+of\\s+\\d+\\s+flights\\b/i.test(text)||/\\b\\d+\\s+flights\\b/i.test(text);"
            "});"
            "return fh.clean(countNode?.innerText);};"
            "fh.summary=()=>{"
            "const tabText=label=>(fh.findSort(label)?.innerText||'').trim();"
            "return {cheapest:tabText('cheapest'),best:tabText('best'),quickest:tabText('quickest')};"
            "};"
            "fh.priceRe=/(?:CA?\\$|US\\$|A\\$|NZ\\$|S\\$|HK\\$|€|£|¥|₹|CHF|\\$)\\s?\\d[\\d,]*/i;"
            "fh.timeHits=text=>(fh.clean(text).match(/\\b\\d{1,2}:\\d{2}\\s*[ap]m\\b/gi)||[]).length;"
            "fh.legNodes=card=>Array.from(card.querySelectorAll('ol.hJSA-list>li,ol li,[role=\"listitem\"],li')).filter(li=>fh.visible(li)&&fh.clean(li.innerText)&&fh.timeHits(li.innerText)>=2).slice(0,2);"
            "fh.priceText=card=>fh.clean(card.querySelector('.nrc6-price-section .e2GB-price-text,[class*=\"price-text\"],[data-testid*=\"price\"]')?.innerText)||fh.clean((fh.clean(card.innerText).match(fh.priceRe)||[])[0]);"
            "fh.findCardRoot=node=>{"
            "for(let depth=0;node&&depth<8;depth+=1,node=node.parentElement){"
            "const text=fh.clean(node.innerText);"
            "if(!text)continue;"
            "if(fh.priceText(node)&&(node.querySelectorAll('ol.hJSA-list > li').length>=2||fh.legNodes(node).length>=2||fh.timeHits(text)>=4))return node;"
            "}"
            "return null;};"
            f"fh.captureCards=()=>{{const cardLimit={card_limit};"
            "const isCard=node=>!!node&&fh.visible(node)&&!!fh.priceText(node)&&(node.querySelectorAll('ol.hJSA-list > li').length>=2||fh.legNodes(node).length>=2||fh.timeHits(node.innerText)>=4);"
            "const selectorMatches=Array.from(document.querySelectorAll('div[aria-label^=\"Result item\"],div[data-resultid],div.nrc6,div[class*=\"nrc6\"],article,section')).filter(isCard);"
            "const buttonMatches=Array.from(document.querySelectorAll('button,a')).filter(el=>fh.visible(el)&&/\\bselect\\b/i.test(fh.clean(el.innerText||el.getAttribute('aria-label')))).map(fh.findCardRoot).filter(isCard);"
            "const raw=Array.from(new Set(selectorMatches.concat(buttonMatches)));"
            "const roots=raw.filter((card,index)=>!raw.some((other,otherIndex)=>otherIndex!==index&&card.contains(other)&&isCard(other)));"
            "return {"
            "card_count:roots.length,"
            "captured_count:roots.slice(0,cardLimit).length,"
            "cards:roots.slice(0,cardLimit).map(card=>({"
            "text:fh.clean(card.innerText),"
            "price_text:fh.priceText(card),"
            "booking_href:fh.clean(card.querySelector('.nrc6-price-section a[href*=\"/book/\"]')?.getAttribute('href')),"
            "cabin:fh.clean(card.querySelector('.nrc6-price-section .Hy6H')?.innerText),"
            "airline_text:fh.clean(card.querySelector('.J0g6-operator-text')?.innerText),"
            "badges:Array.from(card.querySelectorAll('span,div,button')).map(node=>fh.clean(node.innerText)).filter(text=>/^(best|cheapest|quickest)$/i.test(text)).slice(0,3),"
            "legs:fh.legNodes(card).map(li=>({"
            "text:fh.clean(li.innerText),"
            "airline:fh.clean(li.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt')),"
            "time_text:fh.clean(li.querySelector('.VY2U .vmXl')?.innerText),"
            "route_text:fh.clean(li.querySelector('.VY2U [dir=\"ltr\"]')?.innerText),"
            "stops_text:fh.clean(li.querySelector('.JWEO .vmXl')?.innerText),"
            "layover_text:fh.clean(li.querySelector('.JWEO .c_cgF')?.innerText),"
            "duration_text:fh.clean(li.querySelector('.xdW8 .vmXl')?.innerText)"
            "})).filter(leg=>leg.text)"
            "}))};};"
            "fh.captureCurrent=label=>{fh.views=fh.views||{};fh.views[label]=fh.captureCards();return true;};"
            "fh.finalize=()=>JSON.stringify({count_text:fh.countText(),summary:fh.summary(),views:fh.views||{}});"
            "return true;"
            "})()"
        )
        click_sort = lambda label: f"window.__fhResults?.clickSort?.('{label}') ?? false"
        capture_sort = lambda label: f"window.__fhResults?.captureCurrent?.('{label}') ?? false"
        finalize_script = "window.__fhResults?.finalize?.() ?? '{}'"
        if not deep:
            return {
                "strict": False,
                "instructions": [
                    {"evaluate": helper_script},
                    {"wait": 4_500},
                    {"evaluate": click_sort("cheapest")},
                    {"wait": 900},
                    {"evaluate": "window.scrollTo(0, 0); true;"},
                    {"wait": 250},
                    {"evaluate": "window.scrollBy(0, 1400); true;"},
                    {"wait": 500},
                    {"evaluate": "window.scrollBy(0, 2600); true;"},
                    {"wait": 600},
                    {"evaluate": capture_sort("cheapest")},
                    {"evaluate": click_sort("best")},
                    {"wait": 900},
                    {"evaluate": "window.scrollTo(0, 0); true;"},
                    {"wait": 250},
                    {"evaluate": "window.scrollBy(0, 1600); true;"},
                    {"wait": 500},
                    {"evaluate": capture_sort("best")},
                    {"evaluate": click_sort("quickest")},
                    {"wait": 900},
                    {"evaluate": "window.scrollTo(0, 0); true;"},
                    {"wait": 250},
                    {"evaluate": "window.scrollBy(0, 1600); true;"},
                    {"wait": 500},
                    {"evaluate": capture_sort("quickest")},
                    {"evaluate": finalize_script},
                ],
            }
        return {
            "strict": False,
            "instructions": [
                {"evaluate": helper_script},
                {"wait": 5_500},
                {"evaluate": click_sort("cheapest")},
                {"wait": 1_100},
                {"evaluate": "window.scrollTo(0, 0); true;"},
                {"wait": 250},
                {"evaluate": "window.scrollBy(0, 1600); true;"},
                {"wait": 600},
                {"evaluate": "window.scrollBy(0, 3200); true;"},
                {"wait": 700},
                {"evaluate": capture_sort("cheapest")},
                {"evaluate": click_sort("best")},
                {"wait": 1_100},
                {"evaluate": "window.scrollTo(0, 0); true;"},
                {"wait": 250},
                {"evaluate": "window.scrollBy(0, 1800); true;"},
                {"wait": 600},
                {"evaluate": "window.scrollBy(0, 3000); true;"},
                {"wait": 700},
                {"evaluate": capture_sort("best")},
                {"evaluate": click_sort("quickest")},
                {"wait": 1_100},
                {"evaluate": "window.scrollTo(0, 0); true;"},
                {"wait": 250},
                {"evaluate": "window.scrollBy(0, 1800); true;"},
                {"wait": 600},
                {"evaluate": "window.scrollBy(0, 3000); true;"},
                {"wait": 700},
                {"evaluate": capture_sort("quickest")},
                {"evaluate": finalize_script},
            ],
        }

    async def _parse_multi_city_rendered_payload(
        self,
        rendered: dict,
        *,
        currency: str,
        deep_link: str,
        market_country_code: str,
    ) -> dict[str, object]:
        cards_payload = self._extract_rendered_cards_payload(rendered)
        if cards_payload is None:
            return {
                "results": [],
                "card_count": 0,
                "captured_count": 0,
                "captured_sorts": [],
                "count_text": "",
            }

        max_card_count = 0
        total_captured_count = 0
        captured_sorts: list[str] = []
        merged_results: dict[tuple[object, ...], ProviderResult] = {}

        for captured_sort, payload in self._multi_city_card_views(cards_payload).items():
            raw_count = payload.get("card_count")
            if isinstance(raw_count, int) and raw_count >= 0:
                max_card_count = max(max_card_count, raw_count)
            raw_captured_count = payload.get("captured_count")
            if isinstance(raw_captured_count, int) and raw_captured_count >= 0:
                total_captured_count += raw_captured_count
                if raw_captured_count > 0:
                    captured_sorts.append(captured_sort)

            view_results = await asyncio.to_thread(
                self._normalize_multi_city_cards,
                payload,
                currency=currency,
                deep_link=deep_link,
                market_country_code=market_country_code,
                captured_sort=captured_sort,
            )

            for result in view_results:
                fingerprint = self._multi_city_result_fingerprint(result)
                existing = merged_results.get(fingerprint)
                if existing is None:
                    merged_results[fingerprint] = result
                else:
                    merged_results[fingerprint] = self._merge_multi_city_results(existing, result)

        return {
            "results": sorted(merged_results.values(), key=lambda item: item.price),
            "card_count": max_card_count,
            "captured_count": total_captured_count,
            "captured_sorts": list(dict.fromkeys(captured_sorts)),
            "count_text": _clean_text(cards_payload.get("count_text")),
        }


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
        capture_incomplete: bool = False,
        rendered_card_count: int = 0,
        rendered_captured_count: int = 0,
        captured_sorts: list[str] | None = None,
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
            capture_incomplete=capture_incomplete,
            rendered_card_count=rendered_card_count,
            rendered_captured_count=rendered_captured_count,
            captured_sorts=list(captured_sorts or []),
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
            if isinstance(payload, dict) and (
                isinstance(payload.get("cards"), list)
                or isinstance(payload.get("views"), dict)
            ):
                return payload
        return None

    def _rendered_payload_has_summary_prices(self, rendered: dict) -> bool:
        payload = self._extract_rendered_cards_payload(rendered)
        if payload is None:
            return False
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            return False
        return any(_clean_text(summary.get(key)) for key in ("cheapest", "best", "quickest"))

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

    def _multi_city_card_views(self, payload: dict[str, object]) -> dict[str, dict[str, object]]:
        raw_views = payload.get("views")
        if isinstance(raw_views, dict):
            views: dict[str, dict[str, object]] = {}
            for label in ("cheapest", "best", "quickest"):
                value = raw_views.get(label)
                if isinstance(value, dict):
                    views[label] = value
            if views:
                return views

        if isinstance(payload.get("cards"), list):
            return {"cheapest": payload}

        return {}

    def _multi_city_result_fingerprint(self, result: ProviderResult) -> tuple[object, ...]:
        raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
        legs = raw_data.get("legs")
        normalized_legs = legs if isinstance(legs, list) else []
        leg_fingerprint: list[tuple[str, str, str, str, str]] = []
        for leg in normalized_legs[:2]:
            if not isinstance(leg, dict):
                continue
            leg_fingerprint.append(
                (
                    _clean_text(leg.get("airline")).lower(),
                    _clean_text(leg.get("time_text")).lower(),
                    _clean_text(leg.get("route_text")).lower(),
                    _clean_text(leg.get("duration_text")).lower(),
                    _clean_text(leg.get("stops_text")).lower(),
                )
            )

        if not leg_fingerprint:
            leg_fingerprint.append(
                (
                    _clean_text(result.airline).lower(),
                    "",
                    "",
                    _clean_text(raw_data.get("duration_text")).lower(),
                    "",
                )
            )

        return (
            round(result.price, 2),
            _clean_text(result.currency).upper(),
            tuple(leg_fingerprint),
        )

    def _merge_multi_city_results(
        self,
        existing: ProviderResult,
        incoming: ProviderResult,
    ) -> ProviderResult:
        existing_raw = existing.raw_data if isinstance(existing.raw_data, dict) else {}
        incoming_raw = incoming.raw_data if isinstance(incoming.raw_data, dict) else {}

        existing_sorts = [
            _clean_text(value).lower()
            for value in (existing_raw.get("captured_sorts") or [])
            if _clean_text(value)
        ]
        incoming_sorts = [
            _clean_text(value).lower()
            for value in (incoming_raw.get("captured_sorts") or [])
            if _clean_text(value)
        ]
        merged_sorts = list(dict.fromkeys(existing_sorts + incoming_sorts))
        if merged_sorts:
            existing_raw["captured_sorts"] = merged_sorts

        existing_badges = [
            _clean_text(value)
            for value in (existing_raw.get("badges") or [])
            if _clean_text(value)
        ]
        incoming_badges = [
            _clean_text(value)
            for value in (incoming_raw.get("badges") or [])
            if _clean_text(value)
        ]
        merged_badges = list(dict.fromkeys(existing_badges + incoming_badges))
        if merged_badges:
            existing_raw["badges"] = merged_badges

        if (
            incoming.deep_link
            and "/book/" in incoming.deep_link
            and (not existing.deep_link or "/book/" not in existing.deep_link)
        ):
            existing.deep_link = incoming.deep_link

        if not _clean_text(existing_raw.get("price_text")) and _clean_text(incoming_raw.get("price_text")):
            existing_raw["price_text"] = incoming_raw.get("price_text")

        if not _clean_text(existing_raw.get("summary")) and _clean_text(incoming_raw.get("summary")):
            existing_raw["summary"] = incoming_raw.get("summary")

        existing.raw_data = existing_raw
        return existing

    def _parse_visible_flight_count(self, count_text: object) -> int | None:
        text = _clean_text(count_text)
        if not text:
            return None
        match = _FLIGHT_COUNT_RE.search(text.replace(",", ""))
        if not match:
            return None
        return int(match.group(2))

    def _is_multi_city_capture_incomplete(
        self,
        *,
        summary_prices: dict[str, str],
        captured_count: int,
        count_text: str,
        captured_sorts: list[str],
    ) -> bool:
        total_flights = self._parse_visible_flight_count(count_text)
        if captured_count <= 0:
            return bool(summary_prices or total_flights)

        if total_flights is not None and total_flights >= max(captured_count * 8, 80) and captured_count < 20:
            return True

        if summary_prices and len(captured_sorts) < 2 and captured_count < 12:
            return True

        return False

    def _results_include_badge(
        self,
        results: list[ProviderResult],
        label: str,
    ) -> bool:
        target = label.strip().lower()
        for result in results:
            raw_data = result.raw_data if isinstance(result.raw_data, dict) else {}
            badges = raw_data.get("badges")
            if not isinstance(badges, list):
                continue
            if any(_clean_text(badge).lower() == target for badge in badges):
                return True
        return False

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
        captured_sorts: list[str],
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
            captured_sorts=captured_sorts,
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
            return_airline = airline_parts[1] if len(airline_parts) > 1 else ""
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

    def _normalize_multi_city_cards(
        self,
        payload: dict,
        *,
        currency: str,
        deep_link: str,
        market_country_code: str,
        captured_sort: str | None = None,
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

            legs = card.get("legs")
            if not isinstance(legs, list) or len(legs) < 2:
                continue

            normalized_legs: list[dict[str, object]] = []
            unique_airlines: list[str] = []
            leg_stop_counts: list[int] = []
            leg_durations: list[int] = []
            total_duration = 0
            for leg in legs[:2]:
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
                normalized_legs.append(
                    {
                        "airline": airline,
                        "time_text": _clean_text(leg.get("time_text")),
                        "route_text": route_text,
                        "stops_text": stops_text,
                        "layover_text": layover_text,
                        "duration_text": duration_text,
                        "duration_minutes": leg_duration,
                        "text": _clean_text(leg.get("text")),
                    }
                )

            if len(normalized_legs) < 2:
                continue

            total_stops = sum(leg_stop_counts)

            airline_text = _clean_text(card.get("airline_text"))
            display_airline = airline_text or " / ".join(unique_airlines) or "Unknown airline"
            booking_href = _clean_text(card.get("booking_href"))
            normalized_link = urljoin(deep_link, booking_href) if booking_href else deep_link
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
                        "trip_type": "multi_city",
                        "duration_text": " / ".join(
                            leg["duration_text"]
                            for leg in normalized_legs
                            if isinstance(leg.get("duration_text"), str) and leg["duration_text"]
                        ),
                        "price_text": _clean_text(card.get("price_text")),
                        "summary": card_text,
                        "cabin": _clean_text(card.get("cabin")),
                        "badges": badges,
                        "legs": normalized_legs,
                        "airline_names": unique_airlines,
                        "leg_stops": leg_stop_counts,
                        "leg_durations": leg_durations,
                        "stop_result_label": self._stop_label_from_leg_stops(leg_stop_counts),
                        "outbound_airline": normalized_legs[0].get("airline") or "",
                        "return_airline": normalized_legs[1].get("airline") or "",
                        "captured_sorts": [captured_sort] if captured_sort else [],
                    },
                )
            )

        return sorted(results, key=lambda item: item.price)

    def _normalize_multi_city_ai_offers(
        self,
        payload: dict,
        *,
        currency: str,
        deep_link: str,
        market_country_code: str,
    ) -> list[ProviderResult]:
        offers = payload.get("offers")
        if not isinstance(offers, list):
            return []

        results: list[ProviderResult] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue

            price = self._parse_price(offer.get("price")) or self._parse_price(offer.get("price_text"))
            if price is None:
                continue

            outbound_airline = _clean_text(offer.get("outbound_airline"))
            return_airline = _clean_text(offer.get("return_airline"))
            airline = _clean_text(offer.get("airline"))
            if not outbound_airline and airline:
                outbound_airline = airline
            if not return_airline and airline:
                return_airline = airline

            outbound_duration_text = _clean_text(offer.get("outbound_duration_text"))
            return_duration_text = _clean_text(offer.get("return_duration_text"))
            outbound_duration = self._parse_duration_minutes("", outbound_duration_text)
            return_duration = self._parse_duration_minutes("", return_duration_text)
            if outbound_duration <= 0 or return_duration <= 0:
                continue

            outbound_stops_text = _clean_text(offer.get("outbound_stops_text"))
            return_stops_text = _clean_text(offer.get("return_stops_text"))
            outbound_stops = self._parse_stops(outbound_stops_text, offer.get("outbound_stops"))
            return_stops = self._parse_stops(return_stops_text, offer.get("return_stops"))

            booking_href = _clean_text(offer.get("link"))
            normalized_link = urljoin(deep_link, booking_href) if booking_href else deep_link
            summary = _clean_text(offer.get("summary"))
            price_text = _clean_text(offer.get("price_text"))
            actual_currency = self._detect_display_currency(
                price_text or summary,
                requested_currency=currency,
                market_country_code=market_country_code,
            )

            legs = [
                {
                    "airline": outbound_airline,
                    "time_text": _clean_text(offer.get("outbound_time_text")),
                    "route_text": _clean_text(offer.get("outbound_route_text")),
                    "stops_text": outbound_stops_text or self._stop_label_for_count(outbound_stops),
                    "layover_text": "",
                    "duration_text": outbound_duration_text,
                    "duration_minutes": outbound_duration,
                    "text": " ".join(
                        part
                        for part in [
                            _clean_text(offer.get("outbound_time_text")),
                            _clean_text(offer.get("outbound_route_text")),
                            outbound_stops_text or self._stop_label_for_count(outbound_stops),
                            outbound_duration_text,
                        ]
                        if part
                    ),
                },
                {
                    "airline": return_airline,
                    "time_text": _clean_text(offer.get("return_time_text")),
                    "route_text": _clean_text(offer.get("return_route_text")),
                    "stops_text": return_stops_text or self._stop_label_for_count(return_stops),
                    "layover_text": "",
                    "duration_text": return_duration_text,
                    "duration_minutes": return_duration,
                    "text": " ".join(
                        part
                        for part in [
                            _clean_text(offer.get("return_time_text")),
                            _clean_text(offer.get("return_route_text")),
                            return_stops_text or self._stop_label_for_count(return_stops),
                            return_duration_text,
                        ]
                        if part
                    ),
                },
            ]

            airline_names = [
                name
                for name in [outbound_airline, return_airline]
                if name
            ]
            display_airline = airline or " / ".join(dict.fromkeys(airline_names)) or "Unknown airline"

            results.append(
                ProviderResult(
                    price=price,
                    currency=actual_currency,
                    airline=display_airline,
                    deep_link=normalized_link,
                    provider=self.name,
                    duration_minutes=outbound_duration + return_duration,
                    stops=outbound_stops + return_stops,
                    raw_data={
                        "trip_type": "multi_city",
                        "price_text": price_text,
                        "summary": summary,
                        "cabin": _clean_text(offer.get("cabin")),
                        "badges": ["AI Extract"],
                        "legs": legs,
                        "airline_names": airline_names,
                        "leg_stops": [outbound_stops, return_stops],
                        "leg_durations": [outbound_duration, return_duration],
                        "stop_result_label": self._stop_label_from_leg_stops([outbound_stops, return_stops]),
                        "outbound_airline": outbound_airline,
                        "return_airline": return_airline,
                        "captured_sorts": ["ai_extract"],
                    },
                )
            )

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
            if trip_type != "one_way":
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

    async def _search_one_way_once(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        *,
        market: str | None = None,
        currency: str = "USD",
        js_scenario: dict[str, object] | None = None,
    ) -> list[ProviderResult]:
        if self._quota_blocked():
            raise ProviderQuotaExhaustedError("ScrapingBee quota cooldown active.")

        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_search_url(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            market=market,
            currency=currency,
        )
        payload = await self._get_payload(
            target_url,
            country_code=market_country_code,
            js_scenario=js_scenario,
        )
        results = self._normalize_flights(
            payload,
            currency=currency,
            deep_link=target_url,
            trip_type="one_way",
            market_country_code=market_country_code,
        )
        log.info(
            "scrapingbee_results",
            trip_type="one_way",
            origin=origin,
            destination=destination,
            date=depart_date.isoformat(),
            count=len(results),
            currency=currency,
        )
        return results

    async def _search_round_trip_once(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        return_date: date,
        *,
        market: str | None = None,
        currency: str = "USD",
        js_scenario: dict[str, object] | None = None,
    ) -> list[ProviderResult]:
        if self._quota_blocked():
            raise ProviderQuotaExhaustedError("ScrapingBee quota cooldown active.")

        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_search_url(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            market=market,
            currency=currency,
        )
        payload = await self._get_payload(
            target_url,
            country_code=market_country_code,
            js_scenario=js_scenario,
        )
        results = self._normalize_flights(
            payload,
            currency=currency,
            deep_link=target_url,
            trip_type="round_trip",
            market_country_code=market_country_code,
        )
        log.info(
            "scrapingbee_results",
            trip_type="round_trip",
            origin=origin,
            destination=destination,
            depart_date=depart_date.isoformat(),
            return_date=return_date.isoformat(),
            count=len(results),
            currency=currency,
        )
        return results

    async def _search_multi_city_once(
        self,
        legs: list[dict[str, object]],
        *,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> list[ProviderResult]:
        if self._quota_blocked():
            raise ProviderQuotaExhaustedError("ScrapingBee quota cooldown active.")

        if len(legs) != 2:
            return []

        outbound = legs[0]
        inbound = legs[1]
        outbound_date = outbound.get("outbound_date")
        inbound_date = inbound.get("outbound_date")
        if not isinstance(outbound_date, date) or not isinstance(inbound_date, date):
            return []

        outbound_origin = str(outbound["departure_id"]).upper()
        outbound_destination = str(outbound["arrival_id"]).upper()
        inbound_origin = str(inbound["departure_id"]).upper()
        inbound_destination = str(inbound["arrival_id"]).upper()
        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_multi_city_results_url(
            outbound_origin=outbound_origin,
            outbound_destination=outbound_destination,
            outbound_date=outbound_date,
            inbound_origin=inbound_origin,
            inbound_destination=inbound_destination,
            inbound_date=inbound_date,
            market=market,
            currency=currency,
        )
        self._last_multi_city_capture = self._empty_multi_city_capture_state()

        used_deep_pass = True
        rendered = await self._get_rendered_payload(
            target_url,
            js_scenario=self._build_multi_city_results_scenario(deep=True),
            country_code=market_country_code,
            block_resources=False,
            wait_ms=5000,
        )
        summary_prices = self._multi_city_summary_prices(rendered)
        parsed_payload = await self._parse_multi_city_rendered_payload(
            rendered,
            currency=currency,
            deep_link=target_url,
            market_country_code=market_country_code,
        )
        results = list(parsed_payload["results"])
        card_count = int(parsed_payload["card_count"])
        captured_count = int(parsed_payload["captured_count"])
        captured_sorts = list(parsed_payload["captured_sorts"])
        count_text = str(parsed_payload["count_text"])
        eligible_results = self._filter_results_by_stops(results, max_stops)
        capture_incomplete = self._is_multi_city_capture_incomplete(
            summary_prices=summary_prices,
            captured_count=captured_count,
            count_text=count_text,
            captured_sorts=captured_sorts,
        )
        if not results or capture_incomplete:
            ai_payload = await self._get_payload(
                target_url,
                ai_extract_rules=self._MULTI_CITY_AI_EXTRACT_RULES,
                js_scenario=_DEEP_RESULTS_JS_SCENARIO,
                country_code=market_country_code,
            )
            ai_results = self._normalize_multi_city_ai_offers(
                ai_payload,
                currency=currency,
                deep_link=target_url,
                market_country_code=market_country_code,
            )
            if ai_results:
                results = ai_results
                eligible_results = self._filter_results_by_stops(ai_results, max_stops)
                captured_sorts = ["ai_extract"]
                card_count = max(card_count, len(ai_results))
                captured_count = len(ai_results)
                capture_incomplete = False
        else:
            eligible_results = self._filter_results_by_stops(results, max_stops)
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
            captured_sorts=captured_sorts,
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
            count=len(results),
            eligible_count=len(eligible_results),
            currency=currency,
            target_url=target_url,
            card_count=card_count,
            captured_count=captured_count,
            captured_sorts=captured_sorts,
            capture_incomplete=capture_incomplete,
        )
        self._last_multi_city_capture = {
            "summary_prices": summary_prices,
            "card_count": card_count,
            "captured_count": captured_count,
            "captured_sorts": captured_sorts,
            "capture_incomplete": capture_incomplete,
            "count_text": count_text,
        }
        return results

    async def search_one_way_diagnostic(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> ProviderSearchOutcome:
        del max_stops

        results = await self._search_one_way_once(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            market=market,
            currency=currency,
        )
        used_strong_retry = False
        if not results:
            retry_results = await self._search_one_way_once(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                market=market,
                currency=currency,
                js_scenario=_DEEP_RESULTS_JS_SCENARIO,
            )
            if retry_results:
                results = retry_results
                used_strong_retry = True

        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=market,
            requested_currency=currency,
            result_reason="page_empty" if not results else "success",
            used_strong_retry=used_strong_retry,
        )
        return ProviderSearchOutcome(results=results, diagnostics=diagnostics)

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
    ) -> ProviderSearchOutcome:
        del max_stops

        results = await self._search_round_trip_once(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            market=market,
            currency=currency,
        )
        used_strong_retry = False
        if not results:
            retry_results = await self._search_round_trip_once(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                return_date=return_date,
                market=market,
                currency=currency,
                js_scenario=_DEEP_RESULTS_JS_SCENARIO,
            )
            if retry_results:
                results = retry_results
                used_strong_retry = True

        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=market,
            requested_currency=currency,
            result_reason="page_empty" if not results else "success",
            used_strong_retry=used_strong_retry,
        )
        return ProviderSearchOutcome(results=results, diagnostics=diagnostics)

    async def search_multi_city_diagnostic(
        self,
        *,
        legs: list[dict[str, object]],
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> ProviderSearchOutcome:
        results = await self._search_multi_city_once(
            legs=legs,
            market=market,
            currency=currency,
            max_stops=max_stops,
        )
        capture_state = self._last_multi_city_capture if isinstance(self._last_multi_city_capture, dict) else {}
        summary_prices = capture_state.get("summary_prices")
        captured_sorts = capture_state.get("captured_sorts")
        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=market,
            requested_currency=currency,
            result_reason=(
                "extract_failed"
                if not results and bool(capture_state.get("capture_incomplete"))
                else ("page_empty" if not results else "success")
            ),
            visible_results_found=bool(results) or bool(capture_state.get("card_count")),
            summary_price_found=bool(summary_prices),
            capture_incomplete=bool(capture_state.get("capture_incomplete")),
            rendered_card_count=int(capture_state.get("card_count") or 0),
            rendered_captured_count=int(capture_state.get("captured_count") or 0),
            captured_sorts=list(captured_sorts) if isinstance(captured_sorts, list) else [],
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

    async def search_one_way(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> list[ProviderResult]:
        del adults, cabin

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=2, max=12),
            retry=retry_if_exception_type(RuntimeError)
            & retry_if_exception(self._should_retry),
            reraise=True,
        ):
            with attempt:
                outcome = await self.search_one_way_diagnostic(
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    market=market,
                    currency=currency,
                    max_stops=max_stops,
                )
                return self._filter_results_by_stops(outcome.results, max_stops)

        return []

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
    ) -> list[ProviderResult]:
        del adults, cabin

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
    ) -> list[ProviderResult]:
        del adults, cabin

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

    async def _search_with_failover(self, search_fn) -> list[ProviderResult]:
        last_exc: BaseException | None = None

        for provider in self._ordered_providers():
            try:
                return await search_fn(provider)
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

        return []

    async def search_one_way(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        adults: int = 1,
        cabin: str = "economy",
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
    ) -> list[ProviderResult]:
        return await self._search_with_failover(
            lambda provider: provider.search_one_way(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                adults=adults,
                cabin=cabin,
                market=market,
                currency=currency,
                max_stops=max_stops,
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
    ) -> list[ProviderResult]:
        return await self._search_with_failover(
            lambda provider: provider.search_multi_city(
                legs=legs,
                adults=adults,
                cabin=cabin,
                market=market,
                currency=currency,
                max_stops=max_stops,
            )
        )

    async def close(self) -> None:
        for provider in self._providers:
            await provider.close()
