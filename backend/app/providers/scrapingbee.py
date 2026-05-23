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
from app.utils.airline_codes import normalize_airline

log = get_logger(__name__)

_BASE_URL = "https://app.scrapingbee.com/api/v1"
_KAYAK_DEFAULT_HOST = "www.kayak.com"
_MONEY_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")
_HOURS_MINUTES_RE = re.compile(r"(?i)(\d+)\s*(?:hours|hour|hrs|hr|h)\s*(?:(\d+)\s*(?:minutes|minute|mins|min|m))?")
_MINUTES_ONLY_RE = re.compile(r"(?i)(\d+)\s*(?:minutes|minute|mins|min|m)")
_STOPS_RE = re.compile(r"(?i)\b(\d+)\s+stop(?:s)?\b")
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
_FAST_MULTI_CITY_CARD_LIMIT = 30
_DEEP_MULTI_CITY_CARD_LIMIT = 180
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
        params["wait"] = 2500

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

    def _build_multi_city_results_scenario(
        self,
        *,
        deep: bool = False,
        same_airline_only: bool = False,
    ) -> dict[str, object]:
        card_limit = _DEEP_MULTI_CITY_CARD_LIMIT if deep else _FAST_MULTI_CITY_CARD_LIMIT
        helper_script = f"""
(()=>{{
const sameAirlineMode={'true' if same_airline_only else 'false'};
const cardLimit={card_limit};
const requiredHits=sameAirlineMode?3:2;
const fh=window.__fhResults||(window.__fhResults={{}});
fh.clean=v=>(v||'').toString().replace(/\\s+/g,' ').trim();
fh.cleanLines=v=>(v||'').toString().replace(/\\u00a0/g,' ').replace(/\\r/g,'').split(/\\n+/).map(part=>fh.clean(part)).filter(Boolean);
fh.visible=el=>{{if(!el)return false;const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&window.getComputedStyle(el).visibility!=='hidden'&&window.getComputedStyle(el).display!=='none';}};
fh.parsePrice=text=>{{const raw=fh.clean(text).replace(/,/g,'');const match=raw.match(/(?:[A-Z]{{0,3}}\\$|[$€£₹])\\s*([0-9]+(?:\\.[0-9]+)?)/i);return match?Number(match[1]):null;}};
fh.clickCheapest=()=>{{
  const pick=Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
    .filter(el=>fh.visible(el)&&/^cheapest(?:\\s|$)/.test(fh.clean(el.innerText||el.getAttribute('aria-label')).toLowerCase()));
  if(!pick.length)return false;
  const target=pick[0].closest('button,a,[role="button"]')||pick[0];
  target.click();
  return true;
}};
fh.loadingActive=()=>Array.from(document.querySelectorAll('[role="progressbar"],progress,[aria-busy="true"],[class*="loading"],[class*="progress"]')).some(el=>fh.visible(el));
fh.airlineFacetRoot=()=>{{
  const candidates=Array.from(document.querySelectorAll('section,aside,div')).filter(el=>{{
    if(!fh.visible(el))return false;
    const text=el.innerText||'';
    return /(^|\\n)\\s*Airlines\\s*($|\\n)/i.test(text) && /(?:[A-Z]{{0,3}}\\$|[$€£₹])\\s*\\d/.test(text);
  }});
  if(!candidates.length)return null;
  candidates.sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length);
  return candidates[0];
}};
fh.airlineFacetOptions=()=>{{
  const root=fh.airlineFacetRoot();
  if(!root)return [];
  const blocked=/^(select all|clear all|show \\d+ more airlines?|alliance|airports|times|take-off|landing|book now, pay later|transportation options)$/i;
  const mixed=/(multiple airlines|multiple airline|mixed airlines|various airlines)/i;
  const seen=new Map();
  for(const el of Array.from(root.querySelectorAll('label,button,[role="button"],li,div,span'))){{
    if(!fh.visible(el))continue;
    const lines=fh.cleanLines(el.innerText);
    if(!lines.length||lines.length>3)continue;
    const rowText=lines.join(' | ');
    const price=fh.parsePrice(rowText);
    if(price===null)continue;
    let name=lines.find(line=>fh.parsePrice(line)===null)||'';
    name=fh.clean(name.replace(/\\b\\d+\\b/g,''));
    if(!name||blocked.test(name)||mixed.test(name))continue;
    const clickable=el.closest('label,button,[role="button"],li')||el;
    const key=name.toLowerCase();
    const existing=seen.get(key);
    if(!existing||price<existing.price)seen.set(key,{{name,price,el:clickable,text:rowText}});
  }}
  return Array.from(seen.values()).sort((a,b)=>a.price-b.price).slice(0,4);
}};
fh.applyAirlineFacet=()=>{{
  if(!sameAirlineMode)return false;
  const options=fh.airlineFacetOptions();
  window.__fhFacetState={{selected:'',options:options.map(option=>({{name:option.name,price:option.price,text:option.text}}))}};
  if(!options.length)return false;
  const target=options[0];
  window.__fhFacetState.selected=target.name;
  const checkbox=target.el.querySelector('input[type="checkbox"]');
  if(checkbox && !checkbox.checked){{checkbox.click();return true;}}
  target.el.click();
  return true;
}};
fh.settle=()=>{{
  const topPrices=Array.from(document.querySelectorAll('.nrc6-price-section .e2GB-price-text')).map(node=>fh.clean(node.innerText)).filter(Boolean).slice(0,6).join('|');
  const cardPrices=Array.from(document.querySelectorAll('.nrc6-price-section .e2GB-price-text')).map(node=>fh.clean(node.innerText)).filter(Boolean).slice(0,6).join('|');
  const countNode=Array.from(document.querySelectorAll('body *')).find(el=>{{
    if(!fh.visible(el))return false;
    const text=fh.clean(el.innerText);
    return /\\b\\d+\\s+of\\s+\\d+\\s+flights\\b/i.test(text)||/\\b\\d+\\s+flights\\b/i.test(text);
  }});
  const countText=fh.clean(countNode?.innerText);
  const summaryText=Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
    .filter(fh.visible)
    .map(el=>fh.clean(el.innerText||el.getAttribute('aria-label')))
    .filter(text=>/^cheapest(?:\\s|$)|^best(?:\\s|$)|^quickest(?:\\s|$)/i.test(text))
    .slice(0,3).join('|');
  const facetState=window.__fhFacetState?.options||fh.airlineFacetOptions().map(option=>({{name:option.name,price:option.price}}));
  const facetPrices=facetState.map(option=>`${{option.name}}:${{option.price}}`).slice(0,6).join('|');
  const selectedFacet=fh.clean(window.__fhFacetState?.selected||'');
  const cardCount=document.querySelectorAll('.nrc6-price-section .e2GB-price-text').length;
  const loading=fh.loadingActive()?'1':'0';
  const key=[loading,selectedFacet,countText,summaryText,topPrices,cardPrices,facetPrices,cardCount].join('||');
  const state=window.__fhSettleState||{{key:'',hits:0}};
  if(key&&key===state.key){{state.hits+=1;}}else{{state.key=key;state.hits=0;}}
  state.loading=loading==='1';
  window.__fhSettleState=state;
  return loading==='0' && (!!summaryText || cardCount>0) && (!!cardPrices || !!topPrices) && state.hits>=requiredHits;
}};
fh.extract=()=>{{
  const isCard=node=>!!node&&!!node.querySelector('.nrc6-price-section .e2GB-price-text')&&node.querySelectorAll('ol.hJSA-list > li').length>=2;
  const raw=Array.from(document.querySelectorAll('div[aria-label^="Result item"],div[data-resultid],div.nrc6,div[class*="nrc6"]')).filter(isCard);
  const roots=raw.filter((card,index)=>!raw.some((other,otherIndex)=>otherIndex!==index&&card.contains(other)&&isCard(other)));
  const tabText=label=>(Array.from(document.querySelectorAll('button,a,[role="button"],div,span')).find(el=>new RegExp('^'+label+'(?:\\\\s|$)').test(fh.clean(el.innerText||el.getAttribute('aria-label')).toLowerCase()))?.innerText||'').trim();
  const facetOptions=(window.__fhFacetState?.options||fh.airlineFacetOptions().map(option=>({{name:option.name,price:option.price,text:option.text}})));
  return JSON.stringify({{
    card_count:roots.length,
    captured_count:roots.slice(0,cardLimit).length,
    cards:roots.slice(0,cardLimit).map(card=>({{
      text:fh.clean(card.innerText),
      price_text:fh.clean(card.querySelector('.nrc6-price-section .e2GB-price-text')?.innerText),
      booking_href:fh.clean(card.querySelector('.nrc6-price-section a[href*="/book/"]')?.getAttribute('href')),
      cabin:fh.clean(card.querySelector('.nrc6-price-section .Hy6H')?.innerText),
      airline_text:fh.clean(card.querySelector('.J0g6-operator-text')?.innerText),
      badges:Array.from(card.querySelectorAll('span,div,button')).map(node=>fh.clean(node.innerText)).filter(text=>/^(best|cheapest|quickest)$/i.test(text)).slice(0,3),
      legs:Array.from(card.querySelectorAll('ol.hJSA-list > li')).map(li=>({{
        text:fh.clean(li.innerText),
        airline:fh.clean(li.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt')),
        time_text:fh.clean(li.querySelector('.VY2U .vmXl')?.innerText),
        route_text:fh.clean(li.querySelector('.VY2U [dir="ltr"]')?.innerText),
        stops_text:fh.clean(li.querySelector('.JWEO .vmXl')?.innerText),
        layover_text:fh.clean(li.querySelector('.JWEO .c_cgF')?.innerText),
        duration_text:fh.clean(li.querySelector('.xdW8 .vmXl')?.innerText)
      }})).filter(leg=>leg.text)
    }})),
    summary:{{cheapest:tabText('cheapest'),best:tabText('best'),quickest:tabText('quickest')}},
    facet:{{selected:fh.clean(window.__fhFacetState?.selected||''),options:facetOptions}},
    settled:!!(window.__fhSettleState&&window.__fhSettleState.hits>=requiredHits&&!window.__fhSettleState.loading),
    same_airline_mode:sameAirlineMode
  }});
}};
return true;
}})()
""".strip()
        helper_script = """
(()=>{{
const m=__MODE__,l=__LIMIT__,h=m?3:2,w=window.__fhR||(window.__fhR={});
w.t=v=>(v||'').toString().replace(/\\s+/g,' ').trim();
w.n=v=>(v||'').toString().replace(/\\u00a0/g,' ').split(/\\n+/).map(w.t).filter(Boolean);
w.v=e=>{{if(!e)return 0;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!='hidden'&&s.display!='none';}};
w.p=v=>{{const m=w.t(v).replace(/,/g,'').match(/(?:[A-Z]{{0,3}}\\$|[$â‚¬Â£â‚¹])\\s*([0-9]+(?:\\.[0-9]+)?)/i);return m?Number(m[1]):null;}};
w.g=()=>Array.from(document.querySelectorAll('.nrc6-price-section .e2GB-price-text')).map(n=>w.t(n.innerText)).filter(Boolean).slice(0,6);
__CLICK__
w.b=()=>Array.from(document.querySelectorAll('[role="progressbar"],progress,[aria-busy="true"],[class*="loading"],[class*="progress"]')).some(w.v);
w.r=()=>Array.from(document.querySelectorAll('section,aside,div')).filter(e=>w.v(e)&&/(^|\\n)\\s*Airlines\\s*($|\\n)/i.test(e.innerText||'')&&/(?:[A-Z]{{0,3}}\\$|[$â‚¬Â£â‚¹])\\s*\\d/.test(e.innerText||'')).sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length)[0]||null;
w.o=()=>{{const r=w.r();if(!r)return[];const x=/^(select all|clear all|show \\d+ more airlines?|alliance|airports|times|take-off|landing|book now, pay later|transportation options)$/i,y=/(multiple airlines|multiple airline|mixed airlines|various airlines)/i,s=new Map();for(const e of Array.from(r.querySelectorAll('label,button,[role="button"],li,div,span'))){if(!w.v(e))continue;const a=w.n(e.innerText);if(!a.length||a.length>3)continue;const j=a.join('|'),p=w.p(j);if(p===null)continue;let n=a.find(z=>w.p(z)===null)||'';n=w.t(n.replace(/\\b\\d+\\b/g,''));if(!n||x.test(n)||y.test(n))continue;const c=e.closest('label,button,[role="button"],li')||e,k=n.toLowerCase(),u=s.get(k);if(!u||p<u.p)s.set(k,{{n,p,e:c}});}return Array.from(s.values()).sort((a,b)=>a.p-b.p).slice(0,4);}};
w.f=()=>{{if(!m)return 0;const o=w.o();window.__fhF={{s:'',o:o.map(x=>({{n:x.n,p:x.p}}))}};if(!o.length)return 0;const t=o[0];window.__fhF.s=t.n;const c=t.e.querySelector('input[type="checkbox"]');if(c&&!c.checked){{c.click();return 1;}}t.e.click();return 1;}};
w.s=()=>{{const p=w.g().join('|'),y=Array.from(document.querySelectorAll('button,a,[role="button"],div,span')).filter(w.v).map(e=>w.t(e.innerText||e.getAttribute('aria-label'))).filter(v=>/^(cheapest|best|quickest)(\\s|$)/i.test(v)).slice(0,3).join('|'),o=(window.__fhF?.o||w.o().map(x=>({{n:x.n,p:x.p}}))).map(x=>`${{x.n}}:${{x.p}}`).join('|'),k=[w.b()?1:0,window.__fhF?.s||'',y,p,o].join('||'),st=window.__fhS||{{k:'',h:0}};st.h=k&&k===st.k?st.h+1:0;st.k=k;st.b=w.b()?1:0;window.__fhS=st;return !st.b&&p&&st.h>=h;}};
w.x=()=>{{const d=n=>n&&n.querySelector('.nrc6-price-section .e2GB-price-text')&&n.querySelectorAll('ol.hJSA-list>li').length>=2,r=Array.from(document.querySelectorAll('div[aria-label^="Result item"],div[data-resultid],div.nrc6,div[class*="nrc6"]')).filter(d),u=r.filter((c,i)=>!r.some((o,j)=>j!==i&&c.contains(o)&&d(o))),tt=q=>(Array.from(document.querySelectorAll('button,a,[role="button"],div,span')).find(e=>new RegExp('^'+q+'(?:\\\\s|$)','i').test(w.t(e.innerText||e.getAttribute('aria-label'))))?.innerText||'').trim();return JSON.stringify({{n:u.length,m:u.slice(0,l).length,c:u.slice(0,l).map(c=>({{t:w.t(c.innerText),p:w.t(c.querySelector('.nrc6-price-section .e2GB-price-text')?.innerText),h:w.t(c.querySelector('.nrc6-price-section a[href*="/book/"]')?.getAttribute('href')),cb:w.t(c.querySelector('.nrc6-price-section .Hy6H')?.innerText),a:w.t(c.querySelector('.J0g6-operator-text')?.innerText),b:Array.from(c.querySelectorAll('span,div,button')).map(n=>w.t(n.innerText)).filter(v=>/^(best|cheapest|quickest)$/i.test(v)).slice(0,3),l:Array.from(c.querySelectorAll('ol.hJSA-list>li')).map(i=>({{t:w.t(i.innerText),a:w.t(i.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt')),tm:w.t(i.querySelector('.VY2U .vmXl')?.innerText),r:w.t(i.querySelector('.VY2U [dir="ltr"]')?.innerText),s:w.t(i.querySelector('.JWEO .vmXl')?.innerText),ly:w.t(i.querySelector('.JWEO .c_cgF')?.innerText),d:w.t(i.querySelector('.xdW8 .vmXl')?.innerText)}})).filter(i=>i.t)}})),s:{{c:tt('cheapest'),b:tt('best'),q:tt('quickest')}},f:{{s:w.t(window.__fhF?.s||''),o:window.__fhF?.o||w.o().map(x=>({{n:x.n,p:x.p}}))}},e:!!(window.__fhS&&window.__fhS.h>=h&&!window.__fhS.b),sm:m}});}};
return true;
})()
""".replace("{{", "{").replace("}}", "}").replace("__MODE__", "true" if same_airline_only else "false").replace("__LIMIT__", str(card_limit)).replace("__CLICK__", "" if same_airline_only else """w.c=()=>{const e=Array.from(document.querySelectorAll('button,a,[role="button"],div,span')).find(n=>w.v(n)&&/^cheapest(?:\\s|$)/i.test(w.t(n.innerText||n.getAttribute('aria-label'))));if(!e)return 0;(e.closest('button,a,[role="button"]')||e).click();return 1;};""").strip()
        click_cheapest_script = "window.__fhR.c()"
        apply_airline_script = "window.__fhR.f()"
        settle_script = "window.__fhR.s()"
        script = "window.__fhR.x()"
        if not deep:
            instructions = [
                {"evaluate": helper_script},
                {"wait": 35_000 if same_airline_only else 5_000},
            ]
            if same_airline_only:
                instructions.extend(
                    [
                        {"evaluate": apply_airline_script},
                        {"wait": 1_200},
                        {"evaluate": "window.scrollBy(0,1200)"},
                        {"wait": 800},
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": settle_script},
                        {"evaluate": script},
                    ]
                )
            else:
                instructions.extend(
                    [
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": apply_airline_script},
                        {"wait": 1_200},
                        {"evaluate": click_cheapest_script},
                        {"wait": 1_000},
                        {"evaluate": "window.scrollBy(0,1200)"},
                        {"wait": 800},
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": settle_script},
                        {"wait": 1_000},
                        {"evaluate": settle_script},
                        {"evaluate": script},
                    ]
                )
            return {
                "strict": False,
                "instructions": instructions,
            }
        instructions = [
            {"evaluate": helper_script},
            {"wait": 35_000 if same_airline_only else 6_500},
        ]
        if same_airline_only:
            instructions.extend(
                [
                    {"evaluate": apply_airline_script},
                    {"wait": 1_200},
                    {"evaluate": "window.scrollBy(0,1800)"},
                    {"wait": 800},
                    {"evaluate": "window.scrollBy(0,2800)"},
                    {"wait": 800},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"evaluate": script},
                ]
            )
        else:
            instructions.extend(
                [
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": apply_airline_script},
                    {"wait": 1_200},
                    {"evaluate": click_cheapest_script},
                    {"wait": 1_200},
                    {"evaluate": "window.scrollBy(0,1200)"},
                    {"wait": 800},
                    {"evaluate": "window.scrollBy(0,1800)"},
                    {"wait": 800},
                    {"evaluate": click_cheapest_script},
                    {"evaluate": "window.scrollBy(0,2400)"},
                    {"wait": 800},
                    {"evaluate": "window.scrollBy(0,2800)"},
                    {"wait": 800},
                    {"evaluate": "window.scrollBy(0,3200)"},
                    {"wait": 800},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"wait": 1_000},
                    {"evaluate": settle_script},
                    {"evaluate": script},
                ]
            )
        return {
            "strict": False,
            "instructions": instructions,
        }

    def _build_results_scenario(
        self,
        *,
        deep: bool = False,
        same_airline_only: bool = False,
        minimum_leg_count: int = 1,
    ) -> dict[str, object]:
        scenario = self._build_multi_city_results_scenario(
            deep=deep,
            same_airline_only=same_airline_only,
        )
        if minimum_leg_count == 2:
            return scenario

        instructions: list[dict[str, object]] = []
        for instruction in scenario["instructions"]:
            cloned = dict(instruction)
            evaluate = cloned.get("evaluate")
            if isinstance(evaluate, str):
                evaluate = evaluate.replace(
                    "querySelectorAll('ol.hJSA-list > li').length>=2",
                    f"querySelectorAll('ol.hJSA-list > li').length>={minimum_leg_count}",
                )
                evaluate = evaluate.replace(
                    "querySelectorAll('ol.hJSA-list>li').length>=2",
                    f"querySelectorAll('ol.hJSA-list>li').length>={minimum_leg_count}",
                )
                cloned["evaluate"] = evaluate
            instructions.append(cloned)

        return {
            "strict": bool(scenario.get("strict", False)),
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

    async def _parse_multi_city_rendered_payload(
        self,
        rendered: dict,
        *,
        currency: str,
        deep_link: str,
        market_country_code: str,
    ) -> tuple[list[ProviderResult], int, int]:
        return await self._parse_rendered_payload(
            rendered,
            currency=currency,
            deep_link=deep_link,
            market_country_code=market_country_code,
            trip_type="multi_city",
            expected_leg_count=2,
        )

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

            if len(normalized_legs) < expected_leg_count:
                continue

            total_stops = sum(leg_stop_counts)

            airline_text = _clean_text(card.get("airline_text"))
            airline_parts = [part.strip() for part in airline_text.split("/") if part.strip()]
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
                        "trip_type": trip_type,
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
            if trip_type != "one_way":
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

    def _normalize_multi_city_cards(
        self,
        payload: dict,
        *,
        currency: str,
        deep_link: str,
        market_country_code: str,
    ) -> list[ProviderResult]:
        return self._normalize_rendered_cards(
            payload,
            currency=currency,
            deep_link=deep_link,
            trip_type="multi_city",
            market_country_code=market_country_code,
            expected_leg_count=2,
        )

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
        deep: bool = False,
        same_airline_only: bool = False,
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
        rendered, _, results, _, _ = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=currency,
            trip_type="one_way",
            minimum_leg_count=1,
            deep=deep,
            same_airline_only=same_airline_only,
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
        deep: bool = False,
        same_airline_only: bool = False,
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
        rendered, _, results, _, _ = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=currency,
            trip_type="round_trip",
            minimum_leg_count=2,
            deep=deep,
            same_airline_only=same_airline_only,
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
    ) -> tuple[dict, dict[str, str], list[ProviderResult], int, int]:
        rendered = await self._get_rendered_payload(
            target_url,
            js_scenario=self._build_results_scenario(
                deep=deep,
                same_airline_only=same_airline_only,
                minimum_leg_count=minimum_leg_count,
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
        initial_deep = same_airline_only
        rendered, summary_prices, results, card_count, _ = await self._render_results_attempt(
            target_url=target_url,
            country_code=market_country_code,
            currency=requested_currency,
            trip_type=trip_type,
            minimum_leg_count=minimum_leg_count,
            deep=initial_deep,
            same_airline_only=same_airline_only,
        )
        eligible_results = self._filter_results_by_stops(results, max_stops)
        used_strong_retry = False

        if same_airline_only:
            same_airline_results = self._same_airline_results_only(eligible_results)
            if same_airline_results:
                eligible_results = same_airline_results
            else:
                used_strong_retry = True
                fallback_rendered, fallback_summary_prices, fallback_results, fallback_card_count, _ = await self._render_results_attempt(
                    target_url=target_url,
                    country_code=market_country_code,
                    currency=requested_currency,
                    trip_type=trip_type,
                    minimum_leg_count=minimum_leg_count,
                    deep=True,
                    same_airline_only=False,
                )
                fallback_eligible = self._filter_results_by_stops(fallback_results, max_stops)
                fallback_same_airline = self._same_airline_results_only(fallback_eligible)
                if (
                    fallback_same_airline
                    or (not results and fallback_results)
                    or (not results and fallback_card_count > card_count)
                ):
                    rendered = fallback_rendered
                    summary_prices = fallback_summary_prices
                    results = fallback_results
                    eligible_results = fallback_same_airline or fallback_eligible
        else:
            if not results and card_count == 0 and not self._rendered_payload_has_summary_prices(rendered):
                retry_rendered, retry_summary_prices, retry_results, retry_card_count, _ = await self._render_results_attempt(
                    target_url=target_url,
                    country_code=market_country_code,
                    currency=requested_currency,
                    trip_type=trip_type,
                    minimum_leg_count=minimum_leg_count,
                    deep=True,
                    same_airline_only=False,
                )
                if retry_results or retry_card_count > 0 or self._rendered_payload_has_summary_prices(retry_rendered):
                    rendered = retry_rendered
                    summary_prices = retry_summary_prices
                    results = retry_results
                    eligible_results = self._filter_results_by_stops(retry_results, max_stops)
                    used_strong_retry = True

            summary_lowest = self._summary_lowest_price(summary_prices)
            eligible_lowest = min((result.price for result in eligible_results), default=None)
            if summary_lowest is not None and (eligible_lowest is None or summary_lowest + 1 < eligible_lowest):
                retry_rendered, retry_summary_prices, retry_results, _, _ = await self._render_results_attempt(
                    target_url=target_url,
                    country_code=market_country_code,
                    currency=requested_currency,
                    trip_type=trip_type,
                    minimum_leg_count=minimum_leg_count,
                    deep=True,
                    same_airline_only=False,
                )
                retry_eligible = self._filter_results_by_stops(retry_results, max_stops)
                if retry_eligible or retry_results:
                    rendered = retry_rendered
                    summary_prices = retry_summary_prices
                    results = retry_results
                    eligible_results = retry_eligible
                    used_strong_retry = True

        if not results and card_count == 0 and not self._rendered_payload_has_summary_prices(rendered):
            raise ValueError("KAYAK rendered page did not expose extractable result cards.")

        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        visible_results_found = card_count > 0 or facet_option_count > 0
        summary_price_found = bool(summary_prices) or facet_option_count > 0

        if same_airline_only:
            if eligible_results:
                result_reason = "success"
            elif results or visible_results_found:
                result_reason = "filtered_out"
            elif summary_price_found:
                result_reason = "extract_failed"
            else:
                result_reason = "page_empty"
        else:
            result_reason = "success" if eligible_results else ("extract_failed" if (visible_results_found or summary_price_found) else "page_empty")

        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=requested_market,
            requested_currency=requested_currency,
            result_reason=result_reason,
            visible_results_found=visible_results_found,
            summary_price_found=summary_price_found,
            used_strong_retry=used_strong_retry or (same_airline_only and bool(selected_facet)),
        )
        diagnostics.raw_offers_found = len(results)
        diagnostics.eligible_offers_found = len(eligible_results)
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

        used_deep_pass = True
        rendered: dict = {}
        summary_prices: dict[str, str] = {}
        results: list[ProviderResult] = []
        card_count = 0
        captured_count = 0

        async def _render_attempt(*, facet_primary: bool) -> tuple[dict, dict[str, str], list[ProviderResult], int, int]:
            attempt_rendered = await self._get_rendered_payload(
                target_url,
                js_scenario=self._build_multi_city_results_scenario(
                    deep=True,
                    same_airline_only=facet_primary,
                ),
                country_code=market_country_code,
            )
            attempt_summary_prices = self._multi_city_summary_prices(attempt_rendered)
            attempt_results, attempt_card_count, attempt_captured_count = await self._parse_multi_city_rendered_payload(
                attempt_rendered,
                currency=currency,
                deep_link=target_url,
                market_country_code=market_country_code,
            )
            return (
                attempt_rendered,
                attempt_summary_prices,
                attempt_results,
                attempt_card_count,
                attempt_captured_count,
            )

        rendered, summary_prices, results, card_count, captured_count = await _render_attempt(
            facet_primary=same_airline_only
        )
        eligible_results = self._filter_results_by_stops(results, max_stops)

        if same_airline_only:
            primary_same_airline = self._same_airline_results_only(eligible_results)
            if not primary_same_airline:
                fallback_rendered, fallback_summary_prices, fallback_results, fallback_card_count, fallback_captured_count = await _render_attempt(
                    facet_primary=False
                )
                fallback_eligible = self._filter_results_by_stops(fallback_results, max_stops)
                fallback_same_airline = self._same_airline_results_only(fallback_eligible)
                if (
                    fallback_same_airline
                    or (not results and fallback_results)
                    or (not results and fallback_card_count > card_count)
                ):
                    rendered = fallback_rendered
                    summary_prices = fallback_summary_prices
                    results = fallback_results
                    card_count = fallback_card_count
                    captured_count = fallback_captured_count
                    eligible_results = fallback_eligible
        else:
            for deep_attempt in range(2):
                if results or card_count > 0 or self._rendered_payload_has_summary_prices(rendered):
                    break
                if deep_attempt == 0:
                    rendered, summary_prices, results, card_count, captured_count = await _render_attempt(
                        facet_primary=False
                    )
                    eligible_results = self._filter_results_by_stops(results, max_stops)

            summary_lowest = self._summary_lowest_price(summary_prices)
            eligible_lowest = min((result.price for result in eligible_results), default=None)
            if (
                summary_lowest is not None
                and (eligible_lowest is None or summary_lowest + 1 < eligible_lowest)
            ):
                retry_rendered, retry_summary_prices, retry_results, retry_card_count, retry_captured_count = await _render_attempt(
                    facet_primary=False
                )
                retry_eligible = self._filter_results_by_stops(retry_results, max_stops)
                if retry_eligible or retry_results:
                    rendered = retry_rendered
                    summary_prices = retry_summary_prices
                    results = retry_results
                    card_count = retry_card_count
                    captured_count = retry_captured_count
                    eligible_results = retry_eligible

        if not results and card_count == 0 and not self._rendered_payload_has_summary_prices(rendered):
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
        )
        selected_facet, facet_option_count = self._multi_city_facet_snapshot(rendered)
        visible_results_found = card_count > 0 or facet_option_count > 0
        summary_price_found = bool(summary_prices) or facet_option_count > 0
        if same_airline_only:
            same_airline_results = self._same_airline_results_only(eligible_results)
            if same_airline_results:
                result_reason = "success"
            elif eligible_results or results:
                result_reason = "filtered_out"
            elif visible_results_found or summary_price_found:
                result_reason = "extract_failed"
            else:
                result_reason = "page_empty"
        else:
            result_reason = "page_empty" if not eligible_results else "success"

        diagnostics = self._diagnostics_for_results(
            results=results,
            requested_market=market,
            requested_currency=currency,
            result_reason=result_reason,
            visible_results_found=visible_results_found,
            summary_price_found=summary_price_found,
            used_strong_retry=same_airline_only and bool(selected_facet),
        )
        diagnostics.raw_offers_found = len(results)
        diagnostics.eligible_offers_found = len(eligible_results)
        return eligible_results, diagnostics

    async def search_one_way_diagnostic(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: date,
        market: str | None = None,
        currency: str = "USD",
        max_stops: int | None = None,
        same_airline_only: bool = False,
    ) -> ProviderSearchOutcome:
        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_search_url(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            market=market,
            currency=currency,
        )
        return await self._search_rendered_itinerary_diagnostic(
            trip_type="one_way",
            target_url=target_url,
            requested_market=market,
            requested_currency=currency,
            market_country_code=market_country_code,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
            minimum_leg_count=1,
        )

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
        market_country_code = self._market_country_code(currency, market)
        target_url = self._build_search_url(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            market=market,
            currency=currency,
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
        results, diagnostics = await self._search_multi_city_once(
            legs=legs,
            market=market,
            currency=currency,
            max_stops=max_stops,
            same_airline_only=same_airline_only,
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
        same_airline_only: bool = False,
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
                    same_airline_only=same_airline_only,
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
        same_airline_only: bool = False,
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
        same_airline_only: bool = False,
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
                same_airline_only=same_airline_only,
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
                same_airline_only=same_airline_only,
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
                same_airline_only=same_airline_only,
            )
        )

    async def close(self) -> None:
        for provider in self._providers:
            await provider.close()
