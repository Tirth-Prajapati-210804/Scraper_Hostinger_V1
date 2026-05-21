"""
Resolve countries, cities, airports, and raw IATA/metro codes to airport codes.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from unicodedata import normalize

_IATA_RE = re.compile(r"^[A-Z0-9]{2,4}$")
_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "location_catalog.json"


def _ascii_fold(value: str) -> str:
    normalized = normalize("NFKD", value)
    return "".join(char for char in normalized if ord(char) < 128)


def _normalize_text(value: object) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        return ""
    lowered = cleaned.casefold()
    folded = _ascii_fold(lowered)
    return folded or lowered


@lru_cache(maxsize=1)
def _load_catalog() -> list[dict[str, object]]:
    if not _DATA_PATH.exists():
        return []
    payload = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _catalog_by_alias() -> dict[str, list[dict[str, object]]]:
    index: dict[str, list[dict[str, object]]] = {}
    for entry in _load_catalog():
        aliases = entry.get("aliases")
        if not isinstance(aliases, list):
            aliases = []
        labels = [entry.get("label"), *aliases]
        for label in labels:
            normalized = _normalize_text(label)
            if not normalized:
                continue
            index.setdefault(normalized, []).append(entry)
    return index


@lru_cache(maxsize=1)
def _catalog_index() -> dict[str, list[dict[str, object]]]:
    return _catalog_by_alias()


def _codes_for_entry(entry: dict[str, object]) -> list[str]:
    raw_codes = entry.get("codes")
    if not isinstance(raw_codes, list):
        return []
    return list(dict.fromkeys(str(code).strip().upper() for code in raw_codes if str(code).strip()))


def _resolve_single(query: str) -> list[str]:
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        return []

    upper = cleaned.upper()
    if _IATA_RE.fullmatch(upper):
        return [upper]

    normalized = _normalize_text(cleaned)
    matches = _catalog_index().get(normalized, [])
    results: list[str] = []
    for entry in matches:
        results.extend(_codes_for_entry(entry))
    return list(dict.fromkeys(results))


def resolve_location(query: str) -> list[str]:
    if not query:
        return []

    parts = [part.strip() for part in str(query).split(",") if part.strip()]
    results: list[str] = []
    for part in parts:
        results.extend(_resolve_single(part))
    return list(dict.fromkeys(results))


def list_known_locations() -> list[str]:
    return sorted({str(entry.get("label")).strip() for entry in _load_catalog() if str(entry.get("label")).strip()})


def _match_score(candidate: str, query: str) -> int | None:
    if candidate == query:
        return 0
    if candidate.startswith(query):
        return 1
    if query in candidate:
        return 2
    return None


def search_location_suggestions(query: str, limit: int = 8) -> list[dict[str, object]]:
    cleaned = _normalize_text(query)
    if not cleaned:
        return []

    suggestions: list[tuple[int, int, int, str, tuple[str, ...], str]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()

    for entry in _load_catalog():
        label = str(entry.get("label") or "").strip()
        kind = str(entry.get("kind") or "location").strip()
        codes = tuple(_codes_for_entry(entry))
        aliases = entry.get("aliases")
        alias_values = [label]
        if isinstance(aliases, list):
            alias_values.extend(str(alias).strip() for alias in aliases if str(alias).strip())

        score = min(
            (
                matched
                for matched in (_match_score(_normalize_text(candidate), cleaned) for candidate in alias_values)
                if matched is not None
            ),
            default=None,
        )
        if score is None or not codes:
            continue

        dedupe_key = (label.casefold(), codes, kind)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        kind_rank = {"country": 0, "city": 1, "airport": 2}.get(kind, 3)
        suggestions.append((score, kind_rank, len(label), label, codes, kind))

    all_codes = sorted({code for _, _, _, _, codes, _ in suggestions for code in codes})
    for code in all_codes:
        score = _match_score(code.casefold(), cleaned)
        if score is None:
            continue
        dedupe_key = (code.casefold(), (code,), "airport_code")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        suggestions.append((score, 4, len(code), code, (code,), "airport_code"))

    suggestions.sort(key=lambda item: (item[0], item[1], item[2], item[3].casefold()))
    return [
        {"label": label, "codes": list(codes), "kind": kind}
        for _, _, _, label, codes, kind in suggestions[:limit]
    ]
