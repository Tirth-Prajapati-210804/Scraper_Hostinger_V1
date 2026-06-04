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
    ProviderSearchDiagnostics,
    ProviderSearchOutcome,
    ProviderResult,
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
_FAST_MULTI_CITY_CARD_LIMIT = 8
_DEEP_MULTI_CITY_CARD_LIMIT = 12
_MAX_AIRLINE_FACET_ATTEMPTS = 3
_RESULT_PRICE_SELECTOR = ".nrc6-price-section .e2GB-price-text"
_SAME_AIRLINE_INITIAL_WAIT_MS = 5_000
_SAME_AIRLINE_RETRY_WAIT_MS = 9_000
# ScrapingBee's internal render budget must stay BELOW the httpx client timeout so
# a render that legitimately uses most of its budget still returns (with proxy /
# browser-startup / response-transfer overhead) before the client gives up.
# Equal budgets caused every slow Kayak page to fail as "ScrapingBee request timed
# out" at ~the client timeout instead of returning an (inspectable) payload.
_RENDER_TIMEOUT_HEADROOM_SECONDS = 35
_MIN_RENDER_TIMEOUT_MS = 20_000
_MAX_RENDER_TIMEOUT_MS = 140_000


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
            "wait_browser": "load",
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

        return data

    def _kayak_query(self, max_stops: int | None) -> str:
        """Kayak results query. The per-leg stop filter is carried in the URL
        (fs=stops=...), like Kayak's own UI, e.g. ?sort=price_a&fs=stops=0,1.

        We intentionally do NOT put airlines=-MULT in the URL. It was tried
        (commit 6847a1a) and worked on some dates, but on others the -MULT URL
        parameter put Kayak into a broken "No matching results" state (0 cards)
        even though same-airline fares existed -- verified on YEG-KEF 2026-10-19,
        where the -MULT URL returned 0 but the facet-untick workflow below
        returned 3 same-airline Alaska cards. Same-airline isolation is therefore
        done in the scenario via applyFacet() (untick "Multiple airlines") + the
        Cheapest sort, which is reliable across dates.
        """
        query = "?sort=price_a"
        if max_stops is not None and max_stops <= 1:
            query += f"&fs=stops={'0' if max_stops <= 0 else '0,1'}"
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
    ) -> str:
        route = f"{origin.upper()}-{destination.upper()}"
        base_url = self._kayak_site_base(currency, market)
        query = self._kayak_query(max_stops)
        if return_date:
            return (
                f"{base_url}/flights/{route}/"
                f"{depart_date.isoformat()}/{return_date.isoformat()}{query}"
            )
        return (
            f"{base_url}/flights/{route}/"
            f"{depart_date.isoformat()}{query}"
        )

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
    ) -> str:
        base_url = self._kayak_site_base(currency, market)
        return (
            f"{base_url}/flights/"
            f"{outbound_origin.upper()}-{outbound_destination.upper()}/"
            f"{outbound_date.isoformat()}/"
            f"{inbound_origin.upper()}-{inbound_destination.upper()}/"
            f"{inbound_date.isoformat()}{self._kayak_query(max_stops)}"
        )

    def _build_results_scenario(
        self,
        *,
        deep: bool = False,
        same_airline_only: bool = False,
        minimum_leg_count: int = 1,
        same_airline_wait_ms: int | None = None,
        airline_facet_index: int = 0,
        max_stops: int | None = None,
    ) -> dict[str, object]:
        del same_airline_only
        card_limit = _DEEP_MULTI_CITY_CARD_LIMIT if deep else _FAST_MULTI_CITY_CARD_LIMIT
        facet_index = max(0, min(int(airline_facet_index), _MAX_AIRLINE_FACET_ATTEMPTS - 1))
        effective_wait_ms = (
            same_airline_wait_ms
            if same_airline_wait_ms is not None
            else _SAME_AIRLINE_INITIAL_WAIT_MS
        )

        helper_script = (
            "(()=>{const l=__LIMIT__,g=__MIN_LEGS__,p='__PRICE_SELECTOR__',j='ol.hJSA-list > li',q='label,button,[role=\"button\"],li',d='section,aside,div',c='div[aria-label^=\"Result item\"],div[data-resultid],div.nrc6,div[class*=\"nrc6\"]',b='button,a,[role=\"button\"],div,span',f=window.FH||(window.FH={});"
            "f.t=v=>(v||'').toString().replace(/\\s+/g,' ').trim();"
            "f.n=v=>(v||'').toString().replace(/\\u00a0/g,' ').split(/\\n+/).map(f.t).filter(Boolean);"
            "f.v=e=>{if(!e)return 0;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!='hidden'&&s.display!='none'};"
            "f.p=v=>{const m=f.t(v).replace(/,/g,'').match(/(?:[A-Z]{0,3}\\$|[$\\u20ac\\u00a3\\u20b9])\\s*([0-9]+(?:\\.[0-9]+)?)/i);return m?Number(m[1]):null};"
            "f.o=()=>Array.from(document.querySelectorAll(d)).filter(e=>f.v(e)&&/(^|\\n)\\s*Airlines\\s*($|\\n)/i.test(e.innerText||'')&&/(?:[A-Z]{0,3}\\$|[$\\u20ac\\u00a3\\u20b9])\\s*\\d/.test(e.innerText||'')).sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length)[0]||null;"
            "f.x=()=>{const r=f.o();if(!r)return[];const s=new Map();for(const e of Array.from(r.querySelectorAll(q+',div,span'))){if(!f.v(e))continue;const a=f.n(e.innerText);if(!a.length||a.length>3)continue;const t=a.join('|'),p=f.p(t);if(p===null)continue;let n=a.find(v=>f.p(v)===null)||'';n=f.t(n.replace(/\\b\\d+\\b/g,''));if(!n||/^(select all|clear all|show \\d+ more)/i.test(n)||/(multiple airlines|mixed airlines|various airlines)/i.test(n))continue;const k=n.toLowerCase(),u=s.get(k),z=e.closest(q)||e;if(!u||p<u.p)s.set(k,{n,p,e:z})}return Array.from(s.values()).sort((a,b)=>a.p-b.p).slice(0,4)};"
            "f.a=()=>{const o=f.x();f.g={s:o.length?(o[__FACET_INDEX__]||o[0]).n:'',o:o.map(v=>({n:v.n,p:v.p}))};const r=f.o();if(!r)return 0;let n=0;for(const e of Array.from(r.querySelectorAll(q))){if(!f.v(e))continue;if(/(multiple|mixed|various) airlines/i.test(f.t(e.innerText))){const h=e.querySelector('input[type=\"checkbox\"]');if(!h||h.checked){(h||e).click();n++}}}return n};"
            "f.l=()=>Array.from(document.querySelectorAll('[role=\"progressbar\"],progress,[aria-busy=\"true\"],[class*=\"loading\"],[class*=\"progress\"]')).some(f.v);"
            "f.cheap=()=>{const e=Array.from(document.querySelectorAll(b)).find(x=>f.v(x)&&/^cheapest$/i.test(f.n(x.innerText)[0]||''));if(e){e.click();return 1}return 0};"
            "f.empty=()=>!f.r().length&&/no result|no flight|no match|couldn.t f|adjust your f/i.test(document.body?.innerText||'');"
            "f.r=()=>Array.from(document.querySelectorAll(c)).filter(n=>n&&n.querySelector(p)&&n.querySelectorAll(j).length>=g).filter((n,i,a)=>!a.some((o,k)=>k!==i&&n.contains(o)&&o.querySelector&&o.querySelector(p)&&o.querySelectorAll(j).length>=g));"
            "f.s=()=>{const z=Array.from(document.querySelectorAll(p)).map(n=>f.t(n.innerText)).filter(Boolean).slice(0,6).join('|'),m=(f.g?.o||f.x().map(v=>({n:v.n,p:v.p}))).map(v=>`${v.n}:${v.p}`).join('|'),k=[f.l()?1:0,f.t(f.g?.s||''),z,m,f.r().length].join('||'),st=f.u||{k:'',h:0};st.h=k&&k===st.k?st.h+1:0;st.k=k;st.b=f.l()?1:0;f.u=st;return !st.b&&(z||m)&&st.h>=3};"
            "f.e=()=>{const r=f.r();return JSON.stringify({n:r.length,m:r.slice(0,l).length,c:r.slice(0,l).map(n=>({t:f.t(n.innerText),p:f.t(n.querySelector(p)?.innerText),h:f.t(n.querySelector('.nrc6-price-section a[href*=\"/book/\"]')?.getAttribute('href')),a:f.t(n.querySelector('.J0g6-operator-text')?.innerText),b:Array.from(n.querySelectorAll('span,div,button')).map(v=>f.t(v.innerText)).filter(v=>/^(best|cheapest|quickest)$/i.test(v)).slice(0,3),l:Array.from(n.querySelectorAll(j)).slice(0,g).map(i=>({t:f.t(i.innerText),a:f.t(i.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt')),tm:f.t(i.querySelector('.VY2U .vmXl')?.innerText),r:f.t(i.querySelector('.VY2U [dir=\"ltr\"]')?.innerText),s:f.t(i.querySelector('.JWEO .vmXl')?.innerText),ly:f.t(i.querySelector('.JWEO .c_cgF')?.innerText),d:f.t(i.querySelector('.xdW8 .vmXl')?.innerText)})).filter(i=>i.t)})),f:{s:f.t(f.g?.s||''),o:f.g?.o||f.x().map(v=>({n:v.n,p:v.p}))},e:!!(f.u&&f.u.h>=3&&!f.u.b),np:f.empty(),sm:true})};f.applyFacet=f.a;f.settle=f.s;f.extract=f.e;f.cheapest=f.cheap;return true})()"
        )
        helper_script = (
            helper_script.replace("__LIMIT__", str(card_limit))
            .replace("__MIN_LEGS__", str(max(1, minimum_leg_count)))
            .replace("__FACET_INDEX__", str(facet_index))
            .replace("__PRICE_SELECTOR__", _RESULT_PRICE_SELECTOR)
        )

        # Stops are applied via the URL (fs=stops=...). The scenario only needs to
        # remove the "Multiple airlines" mixed-carrier bucket (applyFacet) so the
        # cheapest SAME-airline card sorts to the top under sort=price_a.
        instructions: list[dict[str, object]] = [
            {"evaluate": helper_script},
            {"wait_for": _RESULT_PRICE_SELECTOR},
            {"wait": effective_wait_ms},
        ]
        instructions.extend(
            [
                # Same-airline isolation: untick "Multiple airlines" via the
                # facet, then re-assert the Cheapest sort. This is done in the
                # scenario (NOT via an airlines=-MULT URL param) because the URL
                # param glitched some dates to 0 cards; the facet untick + sort
                # reliably surfaces the cheapest same-airline card. (The page is
                # loaded WITHOUT -MULT, so the "Multiple airlines" checkbox is
                # present and applyFacet unticks it correctly.)
                {"evaluate": "window.FH.applyFacet()"},
                {"wait": 1200},
                {"evaluate": "window.FH.cheapest()"},
                {"wait": 1000},
                {"scroll_y": 1200},
                {"wait": 700},
                {"evaluate": "window.FH.settle()"},
                {"wait": 900},
                {"evaluate": "window.FH.settle()"},
                {"wait": 900},
                {"evaluate": "window.FH.settle()"},
            ]
        )
        if deep:
            instructions.extend(
                [
                    {"scroll_y": 2000},
                    {"wait": 700},
                    {"evaluate": "window.FH.settle()"},
                ]
            )
        instructions.extend(
            [
                {"wait": 1600},
            ]
        )
        instructions.append({"evaluate": "window.FH.extract()"})

        return {
            "strict": False,
            "instructions": instructions,
        }

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
    ) -> list[ProviderResult]:
        return self._same_airline_results_only(
            self._filter_results_by_stops(results, max_stops)
        )

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

    def _should_probe_alternate_airline_facets(
        self,
        *,
        rendered: dict,
        eligible_results: list[ProviderResult],
        facet_option_count: int,
    ) -> bool:
        if not eligible_results or facet_option_count <= 1:
            return False

        current_price = self._cheapest_result_price(eligible_results)
        facet_prices = self._facet_option_prices(rendered)
        if current_price is None or not facet_prices:
            return False

        facet_floor = min(facet_prices)
        if facet_floor <= 0:
            return False

        clearly_high = current_price >= facet_floor * 1.20 and current_price >= facet_floor + 150
        sparse_and_high = (
            len(eligible_results) <= 2
            and current_price >= 1500
            and current_price >= facet_floor + 75
        )
        return clearly_high or sparse_and_high

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
                actual_return_origin = _clean_text(normalized_legs[1].get("actual_origin"))
                actual_return_destination = _clean_text(normalized_legs[1].get("actual_destination"))
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
                    return_airline = _clean_text(normalized_legs[1].get("airline")) or (
                        airline_parts[1] if len(airline_parts) > 1 else outbound_airline
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
        airline_facet_index: int = 0,
    ) -> tuple[dict, dict[str, str], list[ProviderResult], int, int]:
        rendered = await self._get_rendered_payload(
            target_url,
            js_scenario=self._build_results_scenario(
                deep=deep,
                same_airline_only=same_airline_only,
                minimum_leg_count=minimum_leg_count,
                same_airline_wait_ms=same_airline_wait_ms,
                airline_facet_index=airline_facet_index,
                max_stops=max_stops,
            ),
            country_code=country_code,
        )
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

    async def _try_alternate_airline_facets(
        self,
        *,
        target_url: str,
        country_code: str,
        currency: str,
        trip_type: str,
        minimum_leg_count: int,
        max_stops: int | None,
        deep: bool,
        same_airline_only: bool,
        same_airline_wait_ms: int | None = None,
        rendered: dict,
        summary_prices: dict[str, str],
        results: list[ProviderResult],
        card_count: int,
        captured_count: int,
        eligible_results: list[ProviderResult],
        raw_offers_found: int,
        facet_option_count: int,
        force: bool = False,
    ) -> tuple[
        dict,
        dict[str, str],
        list[ProviderResult],
        int,
        int,
        list[ProviderResult],
        int,
        bool,
    ]:
        if eligible_results and not force:
            return (
                rendered,
                summary_prices,
                results,
                card_count,
                captured_count,
                eligible_results,
                raw_offers_found,
                False,
            )

        max_attempts = min(facet_option_count, _MAX_AIRLINE_FACET_ATTEMPTS)
        if max_attempts <= 1:
            return (
                rendered,
                summary_prices,
                results,
                card_count,
                captured_count,
                eligible_results,
                raw_offers_found,
                False,
            )

        used_alternate_facet = False
        best_rendered = rendered
        best_summary_prices = summary_prices
        best_results = results
        best_card_count = card_count
        best_captured_count = captured_count
        best_eligible_results = eligible_results
        best_price = self._cheapest_result_price(eligible_results)

        for facet_index in range(1, max_attempts):
            (
                retry_rendered,
                retry_summary_prices,
                retry_results,
                retry_card_count,
                retry_captured_count,
            ) = await self._render_results_attempt(
                target_url=target_url,
                country_code=country_code,
                currency=currency,
                trip_type=trip_type,
                minimum_leg_count=minimum_leg_count,
                deep=deep,
                same_airline_only=same_airline_only,
                max_stops=max_stops,
                same_airline_wait_ms=same_airline_wait_ms,
                airline_facet_index=facet_index,
            )
            raw_offers_found = max(raw_offers_found, len(retry_results))
            used_alternate_facet = True
            retry_eligible_results = self._eligible_same_airline_results(
                retry_results,
                max_stops,
            )
            if retry_eligible_results:
                if force:
                    retry_price = self._cheapest_result_price(retry_eligible_results)
                    if retry_price is not None and (
                        best_price is None or retry_price < best_price
                    ):
                        best_rendered = retry_rendered
                        best_summary_prices = retry_summary_prices
                        best_results = retry_results
                        best_card_count = retry_card_count
                        best_captured_count = retry_captured_count
                        best_eligible_results = retry_eligible_results
                        best_price = retry_price
                    continue
                return (
                    retry_rendered,
                    retry_summary_prices,
                    retry_results,
                    retry_card_count,
                    retry_captured_count,
                    retry_eligible_results,
                    raw_offers_found,
                    used_alternate_facet,
                )

        return (
            best_rendered,
            best_summary_prices,
            best_results,
            best_card_count,
            best_captured_count,
            best_eligible_results,
            raw_offers_found,
            used_alternate_facet,
        )

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
        same_airline_only = True
        initial_deep = True
        rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=requested_currency,
            trip_type=trip_type,
            minimum_leg_count=minimum_leg_count,
            deep=initial_deep,
            same_airline_only=same_airline_only,
            max_stops=max_stops,
        )
        eligible_results = self._eligible_same_airline_results(results, max_stops)
        raw_offers_found = len(results)
        used_strong_retry = False
        used_alternate_facet = False
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)

        should_probe_alternate_facet = self._should_probe_alternate_airline_facets(
            rendered=rendered,
            eligible_results=eligible_results,
            facet_option_count=facet_option_count,
        )

        if (
            should_probe_alternate_facet
            or not eligible_results
        ) and (
            results
            or card_count > 0
            or self._rendered_payload_has_summary_prices(rendered)
        ):
            (
                rendered,
                summary_prices,
                results,
                card_count,
                captured_count,
                eligible_results,
                raw_offers_found,
                used_alternate_facet,
            ) = await self._try_alternate_airline_facets(
                target_url=target_url,
                country_code=market_country_code,
                currency=requested_currency,
                trip_type=trip_type,
                minimum_leg_count=minimum_leg_count,
                max_stops=max_stops,
                deep=initial_deep,
                same_airline_only=same_airline_only,
                rendered=rendered,
                summary_prices=summary_prices,
                results=results,
                card_count=card_count,
                captured_count=captured_count,
                eligible_results=eligible_results,
                raw_offers_found=raw_offers_found,
                facet_option_count=facet_option_count,
                force=should_probe_alternate_facet,
            )

        if (
            not eligible_results
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
                same_airline_only=True,
                max_stops=max_stops,
                same_airline_wait_ms=_SAME_AIRLINE_RETRY_WAIT_MS,
            )
            raw_offers_found = max(raw_offers_found, len(retry_results))
            if retry_results or retry_card_count > 0 or self._rendered_payload_has_summary_prices(retry_rendered):
                rendered = retry_rendered
                summary_prices = retry_summary_prices
                results = retry_results
                card_count = retry_card_count
                captured_count = retry_captured_count
                eligible_results = self._eligible_same_airline_results(retry_results, max_stops)
                used_strong_retry = True
                selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
                should_probe_retry_alternate_facet = self._should_probe_alternate_airline_facets(
                    rendered=rendered,
                    eligible_results=eligible_results,
                    facet_option_count=facet_option_count,
                )
                if not eligible_results or should_probe_retry_alternate_facet:
                    (
                        rendered,
                        summary_prices,
                        results,
                        card_count,
                        captured_count,
                        eligible_results,
                        raw_offers_found,
                        retry_used_alternate_facet,
                    ) = await self._try_alternate_airline_facets(
                        target_url=target_url,
                        country_code=market_country_code,
                        currency=requested_currency,
                        trip_type=trip_type,
                        minimum_leg_count=minimum_leg_count,
                        max_stops=max_stops,
                        deep=True,
                        same_airline_only=same_airline_only,
                        same_airline_wait_ms=_SAME_AIRLINE_RETRY_WAIT_MS,
                        rendered=rendered,
                        summary_prices=summary_prices,
                        results=results,
                        card_count=card_count,
                        captured_count=captured_count,
                        eligible_results=eligible_results,
                        raw_offers_found=raw_offers_found,
                        facet_option_count=facet_option_count,
                        force=should_probe_retry_alternate_facet,
                    )
                    used_alternate_facet = used_alternate_facet or retry_used_alternate_facet
            else:
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
            used_strong_retry=used_strong_retry or used_alternate_facet or bool(selected_facet),
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
            used_alternate_facet=used_alternate_facet,
            actual_route=self._actual_route_label(eligible_results),
            **self._accuracy_audit(
                rendered=rendered,
                summary_prices=summary_prices,
                eligible_results=eligible_results,
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
    ) -> tuple[list[ProviderResult], ProviderSearchDiagnostics]:
        same_airline_only = True
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
            max_stops=max_stops,
        )

        used_deep_pass = True
        rendered: dict = {}
        summary_prices: dict[str, str] = {}
        results: list[ProviderResult] = []
        card_count = 0
        captured_count = 0

        rendered, summary_prices, results, card_count, captured_count = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=currency,
            trip_type="multi_city",
            minimum_leg_count=2,
            deep=used_deep_pass,
            same_airline_only=same_airline_only,
            max_stops=max_stops,
        )
        eligible_results = self._eligible_same_airline_results(results, max_stops)
        raw_offers_found = len(results)
        used_alternate_facet = False
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)

        should_probe_alternate_facet = self._should_probe_alternate_airline_facets(
            rendered=rendered,
            eligible_results=eligible_results,
            facet_option_count=facet_option_count,
        )

        if (
            should_probe_alternate_facet
            or not eligible_results
        ) and (
            results
            or card_count > 0
            or self._rendered_payload_has_summary_prices(rendered)
        ):
            (
                rendered,
                summary_prices,
                results,
                card_count,
                captured_count,
                eligible_results,
                raw_offers_found,
                used_alternate_facet,
            ) = await self._try_alternate_airline_facets(
                target_url=target_url,
                country_code=market_country_code,
                currency=currency,
                trip_type="multi_city",
                minimum_leg_count=2,
                max_stops=max_stops,
                deep=used_deep_pass,
                same_airline_only=same_airline_only,
                rendered=rendered,
                summary_prices=summary_prices,
                results=results,
                card_count=card_count,
                captured_count=captured_count,
                eligible_results=eligible_results,
                raw_offers_found=raw_offers_found,
                facet_option_count=facet_option_count,
                force=should_probe_alternate_facet,
            )

        if (
            not eligible_results
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
                minimum_leg_count=2,
                deep=True,
                same_airline_only=True,
                max_stops=max_stops,
                same_airline_wait_ms=_SAME_AIRLINE_RETRY_WAIT_MS,
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
                eligible_results = self._eligible_same_airline_results(retry_results, max_stops)
                selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
                should_probe_retry_alternate_facet = self._should_probe_alternate_airline_facets(
                    rendered=rendered,
                    eligible_results=eligible_results,
                    facet_option_count=facet_option_count,
                )
                if not eligible_results or should_probe_retry_alternate_facet:
                    (
                        rendered,
                        summary_prices,
                        results,
                        card_count,
                        captured_count,
                        eligible_results,
                        raw_offers_found,
                        retry_used_alternate_facet,
                    ) = await self._try_alternate_airline_facets(
                        target_url=target_url,
                        country_code=market_country_code,
                        currency=currency,
                        trip_type="multi_city",
                        minimum_leg_count=2,
                        max_stops=max_stops,
                        deep=True,
                        same_airline_only=same_airline_only,
                        same_airline_wait_ms=_SAME_AIRLINE_RETRY_WAIT_MS,
                        rendered=rendered,
                        summary_prices=summary_prices,
                        results=results,
                        card_count=card_count,
                        captured_count=captured_count,
                        eligible_results=eligible_results,
                        raw_offers_found=raw_offers_found,
                        facet_option_count=facet_option_count,
                        force=should_probe_retry_alternate_facet,
                    )
                    used_alternate_facet = used_alternate_facet or retry_used_alternate_facet
            else:
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
            actual_route=self._actual_route_label(eligible_results),
            **self._accuracy_audit(
                rendered=rendered,
                summary_prices=summary_prices,
                eligible_results=eligible_results,
            ),
        )
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        visible_results_found = card_count > 0 or facet_option_count > 0
        summary_price_found = bool(summary_prices) or facet_option_count > 0
        if eligible_results:
            result_reason = "success"
        elif eligible_results or results:
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
            used_strong_retry=used_alternate_facet or bool(selected_facet),
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
    ) -> ProviderSearchOutcome:
        same_airline_only = True
        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_search_url(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            market=market,
            currency=currency,
            max_stops=max_stops,
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
    ) -> ProviderSearchOutcome:
        del same_airline_only
        results, diagnostics = await self._search_multi_city_once(
            legs=legs,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=True,
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
    ) -> list[ProviderResult]:
        del adults, cabin
        same_airline_only = True

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
    ) -> list[ProviderResult]:
        del adults, cabin
        same_airline_only = True

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
                same_airline_only=True,
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
    ) -> ProviderSearchOutcome:
        return await self._search_outcome_with_failover(
            lambda provider: provider.search_multi_city_diagnostic(
                legs=legs,
                market=market,
                currency=currency,
                max_stops=max_stops,
                same_airline_only=True,
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
                same_airline_only=True,
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
    ) -> list[ProviderResult]:
        return await self._search_with_failover(
            lambda provider: provider.search_multi_city(
                legs=legs,
                adults=adults,
                cabin=cabin,
                market=market,
                currency=currency,
                max_stops=max_stops,
                same_airline_only=True,
            )
        )

    async def close(self) -> None:
        for provider in self._providers:
            await provider.close()
