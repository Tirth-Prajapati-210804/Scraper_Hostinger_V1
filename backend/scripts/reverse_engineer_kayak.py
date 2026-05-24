"""Inspect rendered KAYAK result pages for reusable data endpoints.

This script is intentionally diagnostic-only. It uses the app's ScrapingBee
configuration style but does not save prices or touch the database.

Example:
    SCRAPINGBEE_API_KEY=... python -m scripts.reverse_engineer_kayak \
        --origin EWR --destination MLA --depart-date 2027-03-12 \
        --return-date 2027-03-20 --market us --currency USD

For open-jaw/multi-city:
    SCRAPINGBEE_API_KEY=... python -m scripts.reverse_engineer_kayak \
        --origin YYZ --destination TIA --depart-date 2026-06-05 \
        --return-origin SPU --return-destination YYZ --return-date 2026-06-18 \
        --market ca --currency CAD
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx

from app.providers.scrapingbee import ScrapingBeeProvider


KEYWORDS = (
    "flight",
    "flights",
    "result",
    "results",
    "poll",
    "search",
    "facet",
    "filter",
    "booking",
    "graphql",
    "api",
    "/s/",
    "/a/",
    "horizon",
)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _build_probe_scenario() -> dict[str, object]:
    hook = (
        "(()=>{const w=window,n=w.__fhNet={f:[],x:[]};"
        "if(!w.__fhFetchHook&&w.fetch){const of=w.fetch;w.fetch=function(...a){"
        "try{n.f.push(String(a[0]?.url||a[0]).slice(0,300))}catch(e){}"
        "return of.apply(this,a)};w.__fhFetchHook=1}"
        "if(!w.__fhXhrHook&&w.XMLHttpRequest){const o=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u,...r){try{n.x.push(String(u).slice(0,300))}catch(e){}"
        "return o.call(this,m,u,...r)};w.__fhXhrHook=1}"
        "return true})()"
    )
    inspect_and_click = (
        "(()=>{const p='.nrc6-price-section .e2GB-price-text',b='button,a,[role=\"button\"],div,span',"
        "t=v=>(v||'').toString().replace(/\\s+/g,' ').trim(),"
        "n=v=>t(v).split(/\\n+/).map(t).filter(Boolean),"
        "v=e=>{if(!e)return 0;const r=e.getBoundingClientRect(),s=getComputedStyle(e);"
        "return r.width>0&&r.height>0&&s.visibility!='hidden'&&s.display!='none'},"
        "pr=v=>{const m=t(v).replace(/,/g,'').match(/(?:[A-Z]{0,3}\\$|[$\\u20ac\\u00a3\\u20b9])\\s*([0-9]+(?:\\.[0-9]+)?)/i);return m?Number(m[1]):null},"
        "air=()=>Array.from(document.querySelectorAll('section,aside,div')).filter(e=>v(e)&&/(^|\\n)\\s*Airlines\\s*($|\\n)/i.test(e.innerText||'')&&/(?:[A-Z]{0,3}\\$|[$\\u20ac\\u00a3\\u20b9])\\s*\\d/.test(e.innerText||'')).sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length)[0]||null,"
        "opts=()=>{const r=air();if(!r)return[];const s=new Map();for(const e of Array.from(r.querySelectorAll('label,button,[role=\"button\"],li,div,span'))){if(!v(e))continue;const a=n(e.innerText);if(!a.length||a.length>3)continue;const tx=a.join('|'),pp=pr(tx);if(pp===null)continue;let nm=a.find(x=>pr(x)===null)||'';nm=t(nm.replace(/\\b\\d+\\b/g,''));if(!nm||/^(select all|clear all|show \\d+ more)/i.test(nm)||/(multiple airlines|mixed airlines|various airlines)/i.test(nm))continue;const k=nm.toLowerCase(),q=e.closest('label,button,[role=\"button\"],li')||e,u=s.get(k);if(!u||pp<u.price)s.set(k,{name:nm,price:pp,el:q})}return Array.from(s.values()).sort((a,b)=>a.price-b.price).slice(0,6)};"
        "const o=opts();window.__fhFacet={selected:'',options:o.map(({name,price})=>({name,price}))};"
        "if(o[0]){window.__fhFacet.selected=o[0].name;const h=o[0].el.querySelector('input[type=\"checkbox\"]');if(h&&!h.checked)h.click();else o[0].el.click()}"
        "return JSON.stringify(window.__fhFacet)})()"
    )
    extract = (
        "(()=>{const kw=__KW__,p='.nrc6-price-section .e2GB-price-text',"
        "c='div[aria-label^=\"Result item\"],div[data-resultid],div.nrc6,div[class*=\"nrc6\"]',"
        "t=v=>(v||'').toString().replace(/\\s+/g,' ').trim(),"
        "res=Array.from(performance.getEntriesByType('resource')).map(e=>({n:e.name,i:e.initiatorType,d:Math.round(e.duration||0)})).filter(e=>kw.some(k=>e.n.toLowerCase().includes(k))).slice(-120),"
        "cards=Array.from(document.querySelectorAll(c)).filter(x=>x.querySelector(p)).slice(0,8).map(x=>({price:t(x.querySelector(p)?.innerText),airline:t(x.querySelector('.J0g6-operator-text')?.innerText),text:t(x.innerText).slice(0,900),legs:Array.from(x.querySelectorAll('ol.hJSA-list > li')).slice(0,4).map(l=>({airline:t(l.querySelector('.tdCx-leg-carrier img')?.getAttribute('alt')),route:t(l.querySelector('.VY2U [dir=\"ltr\"]')?.innerText),stops:t(l.querySelector('.JWEO .vmXl')?.innerText),duration:t(l.querySelector('.xdW8 .vmXl')?.innerText)}))})),"
        "hyd=document.getElementById('__R9_HYDRATE_DATA__'),"
        "out={url:location.href,title:document.title,facet:window.__fhFacet||{},net:window.__fhNet||{},resources:res,cards,hydrate_present:!!hyd,body_sample:t(document.body.innerText).slice(0,1200)};"
        "return JSON.stringify(out)})()"
    ).replace("__KW__", json.dumps(KEYWORDS, separators=(",", ":")))
    return {
        "strict": False,
        "instructions": [
            {"evaluate": hook},
            {"wait": 5000},
            {"evaluate": inspect_and_click},
            {"wait": 1800},
            {"scroll_y": 1200},
            {"wait": 2500},
            {"evaluate": extract},
        ],
    }


def _last_json_payload(rendered: dict) -> dict[str, object]:
    for item in reversed(rendered.get("evaluate_results") or []):
        if not isinstance(item, str):
            continue
        try:
            payload = json.loads(item)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "resources" in payload:
            return payload
    return {}


async def _fetch_probe(provider: ScrapingBeeProvider, target_url: str, country_code: str) -> dict[str, object]:
    params = provider._base_request_params(target_url, country_code=country_code)
    params["json_response"] = "True"
    params["block_resources"] = "False"
    params["js_scenario"] = json.dumps(_build_probe_scenario(), separators=(",", ":"))
    async with provider._request_slot(rendered=True):
        response = await provider._client.get(provider._base_url, params=params)
    provider._raise_for_status(response)
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected ScrapingBee response")
    return data


def _summarize(payload: dict[str, object]) -> dict[str, object]:
    urls: list[str] = []
    for bucket in ("resources",):
        for item in payload.get(bucket) or []:
            if isinstance(item, dict) and isinstance(item.get("n"), str):
                urls.append(item["n"])
    net = payload.get("net") if isinstance(payload.get("net"), dict) else {}
    for key in ("f", "x"):
        for item in net.get(key) or []:
            if isinstance(item, str):
                urls.append(item)
    unique_urls = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    return {
        "url": payload.get("url"),
        "title": payload.get("title"),
        "facet": payload.get("facet"),
        "cards": payload.get("cards"),
        "candidate_urls": unique_urls[:80],
        "hydrate_present": payload.get("hydrate_present"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--depart-date", type=_date, required=True)
    parser.add_argument("--return-date", type=_date, required=True)
    parser.add_argument("--return-origin")
    parser.add_argument("--return-destination")
    parser.add_argument("--market", default="us")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    api_key = os.environ.get("SCRAPINGBEE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: SCRAPINGBEE_API_KEY is required", file=sys.stderr)
        return 1

    provider = ScrapingBeeProvider(
        api_key=api_key,
        country_code="",
        timeout=int(os.environ.get("PROVIDER_TIMEOUT_SECONDS", "90")),
        max_retries=1,
        concurrency_limit=1,
        rendered_concurrency_limit=1,
    )
    try:
        country_code = provider._market_country_code(args.currency, args.market)
        if args.return_origin or args.return_destination:
            target_url = provider._build_multi_city_results_url(
                outbound_origin=args.origin,
                outbound_destination=args.destination,
                outbound_date=args.depart_date,
                inbound_origin=args.return_origin or args.destination,
                inbound_destination=args.return_destination or args.origin,
                inbound_date=args.return_date,
                market=args.market,
                currency=args.currency,
            )
        else:
            target_url = provider._build_search_url(
                origin=args.origin,
                destination=args.destination,
                depart_date=args.depart_date,
                return_date=args.return_date,
                market=args.market,
                currency=args.currency,
            )

        rendered = await _fetch_probe(provider, target_url, country_code)
        payload = _last_json_payload(rendered)
        summary = _summarize(payload)
        summary["target_url"] = target_url
        summary["market_country_code"] = country_code
        text = json.dumps(summary, indent=2, ensure_ascii=False)
        print(text)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        return 0
    finally:
        await provider.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
