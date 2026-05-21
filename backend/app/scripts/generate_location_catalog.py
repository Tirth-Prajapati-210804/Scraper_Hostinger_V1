from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from html import unescape
from pathlib import Path
from unicodedata import normalize


ROW_RE = re.compile(
    r'<tr class="mOJ_-row"><td class="mOJ_-cell">(?P<city>.*?)</td>'
    r'<td class="mOJ_-cell"><a [^>]*>(?P<airport>.*?)</a></td>'
    r'<td class="mOJ_-cell">(?P<code>[A-Z0-9]{3,4})</td></tr>',
    re.IGNORECASE,
)

COUNTRY_SEEDS: dict[str, list[str]] = {
    "Canada": ["YYZ", "YVR", "YUL", "YEG", "YYC", "YOW", "YHZ"],
    "United States": ["JFK", "LAX", "ORD", "MIA", "SFO", "SEA", "ATL"],
    "United Kingdom": ["LHR", "LGW", "MAN", "EDI", "GLA", "BHX"],
    "Ireland": ["DUB", "SNN", "ORK", "NOC"],
    "France": ["CDG", "ORY", "NCE", "LYS", "MRS"],
    "Germany": ["FRA", "MUC", "BER", "DUS", "HAM", "STR"],
    "Italy": ["FCO", "MXP", "LIN", "VCE", "NAP"],
    "Spain": ["MAD", "BCN", "AGP", "PMI", "SVQ"],
    "Portugal": ["LIS", "OPO", "FAO", "FNC"],
    "Netherlands": ["AMS", "EIN", "RTM"],
    "Belgium": ["BRU", "CRL", "ANR"],
    "Switzerland": ["ZRH", "GVA", "BSL"],
    "Austria": ["VIE", "SZG", "INN"],
    "Sweden": ["ARN", "GOT", "MMX"],
    "Norway": ["OSL", "BGO", "TRD"],
    "Denmark": ["CPH", "BLL", "AAL"],
    "Finland": ["HEL", "RVN", "TMP"],
    "Poland": ["WAW", "KRK", "GDN", "WRO"],
    "Czech Republic": ["PRG", "BRQ", "OSR"],
    "Hungary": ["BUD", "DEB"],
    "Greece": ["ATH", "SKG", "HER", "RHO"],
    "Turkey": ["IST", "SAW", "AYT", "ADB"],
    "UAE": ["DXB", "AUH", "SHJ"],
    "India": ["DEL", "BOM", "BLR", "MAA", "HYD", "CCU"],
    "Japan": ["NRT", "HND", "KIX", "CTS", "FUK"],
    "China": ["PEK", "PVG", "SHA", "CAN", "CTU"],
    "Hong Kong": ["HKG"],
    "Taiwan": ["TPE", "KHH"],
    "South Korea": ["ICN", "GMP", "PUS", "CJU"],
    "Thailand": ["BKK", "DMK", "HKT", "CNX", "USM"],
    "Vietnam": ["SGN", "HAN", "DAD", "PQC", "CXR"],
    "Singapore": ["SIN"],
    "Malaysia": ["KUL", "PEN", "BKI", "KCH"],
    "Indonesia": ["CGK", "DPS", "SUB", "LOP"],
    "Philippines": ["MNL", "CEB", "DVO", "MPH"],
    "Australia": ["SYD", "MEL", "BNE", "PER", "ADL"],
    "New Zealand": ["AKL", "CHC", "WLG", "ZQN"],
    "Mexico": ["MEX", "CUN", "GDL", "MTY", "PVR"],
    "Brazil": ["GRU", "CGH", "GIG", "BSB", "SSA"],
    "Argentina": ["EZE", "AEP", "COR", "MDZ"],
    "Chile": ["SCL"],
    "Colombia": ["BOG", "MDE", "CTG", "CLO"],
    "Peru": ["LIM", "CUZ", "AQP"],
    "Dominican Republic": ["SDQ", "PUJ", "STI"],
    "South Africa": ["JNB", "CPT", "DUR"],
    "Morocco": ["CMN", "RAK", "AGA", "FEZ"],
    "Egypt": ["CAI", "HRG", "SSH"],
}

COUNTRY_ALIASES: dict[str, list[str]] = {
    "USA": ["United States"],
    "US": ["United States"],
    "UK": ["United Kingdom"],
    "Great Britain": ["United Kingdom"],
    "Britain": ["United Kingdom"],
    "England": ["United Kingdom"],
    "UAE": ["UAE"],
}


def repair_text(value: str) -> str:
    text = unescape(value).strip()
    if any(token in text for token in ("Ã", "Â", "â")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            repaired = text
        else:
            text = repaired
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def ascii_fold(value: str) -> str:
    normalized = normalize("NFKD", value)
    return "".join(char for char in normalized if ord(char) < 128)


def alias_values(label: str, *extra: str) -> list[str]:
    candidates = [label, ascii_fold(label), *extra]
    seen: set[str] = set()
    aliases: list[str] = []
    for raw in candidates:
        cleaned = repair_text(raw)
        lowered = cleaned.casefold()
        if not cleaned or lowered in seen:
            continue
        seen.add(lowered)
        aliases.append(cleaned)
    return aliases


def build_catalog(html_text: str) -> list[dict[str, object]]:
    city_codes: dict[str, list[str]] = defaultdict(list)
    airport_entries: dict[tuple[str, str], dict[str, object]] = {}

    for match in ROW_RE.finditer(html_text):
        city = repair_text(match.group("city"))
        airport = repair_text(match.group("airport"))
        code = match.group("code").upper()

        if code not in city_codes[city]:
            city_codes[city].append(code)

        key = (airport.casefold(), code)
        airport_entries[key] = {
            "label": airport,
            "kind": "airport",
            "codes": [code],
            "aliases": alias_values(airport, city, code, f"{city} {airport}"),
        }

    entries: list[dict[str, object]] = []
    for city, codes in city_codes.items():
        entries.append(
            {
                "label": city,
                "kind": "city",
                "codes": codes,
                "aliases": alias_values(city, *codes),
            }
        )

    entries.extend(airport_entries.values())

    for label, codes in COUNTRY_SEEDS.items():
        entries.append(
            {
                "label": label,
                "kind": "country",
                "codes": codes,
                "aliases": alias_values(label, *(COUNTRY_ALIASES.get(label, []))),
            }
        )

    for alias, labels in COUNTRY_ALIASES.items():
        for label in labels:
            codes = COUNTRY_SEEDS.get(label)
            if not codes:
                continue
            entries.append(
                {
                    "label": alias,
                    "kind": "country",
                    "codes": codes,
                    "aliases": alias_values(alias, label),
                }
            )

    unique: dict[tuple[str, str, tuple[str, ...]], dict[str, object]] = {}
    for entry in entries:
        label = str(entry["label"]).strip()
        kind = str(entry["kind"]).strip()
        codes = tuple(dict.fromkeys(str(code).strip().upper() for code in entry["codes"] if str(code).strip()))
        aliases = sorted(
            {
                repair_text(alias)
                for alias in entry.get("aliases", [])
                if repair_text(alias)
            },
            key=lambda value: (len(value), value.casefold()),
        )
        unique[(label.casefold(), kind, codes)] = {
            "label": label,
            "kind": kind,
            "codes": list(codes),
            "aliases": aliases,
        }

    return sorted(unique.values(), key=lambda item: (str(item["label"]).casefold(), str(item["kind"])))


def main() -> int:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\tmp\kayak_airports.html")
    output_path = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else Path(__file__).resolve().parents[1] / "data" / "location_catalog.json"
    )

    html_text = input_path.read_text(encoding="utf-8", errors="ignore")
    catalog = build_catalog(html_text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(catalog)} location entries to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
