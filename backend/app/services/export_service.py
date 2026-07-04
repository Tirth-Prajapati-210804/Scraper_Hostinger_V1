from __future__ import annotations

import re
from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from app.core.logging import get_logger
from app.models.all_flight_result import AllFlightResult
from app.models.route_group import RouteGroup
from app.utils.route_segments import combined_origin_for_group

log = get_logger(__name__)
_MISSING_VALUE = "N-A"
_INVALID_SHEET_TITLE_RE = re.compile(r"[\[\]:*?/\\]")

_MAIN_HEADERS = [
    "Date",
    "Return Date",
    "Route",
    "Airport",
    "Nights",
    "Airline",
    "Stop Result",
    "Duration",
    "Flight Price",
]

_MULTI_CITY_HEADERS = [
    "Date",
    "Ending Date",
    "Route",
    "Airport",
    "Nights",
    "Airline",
    "Stop Result",
    "Duration",
    "Flight Price",
]

def _display_airport(actual: object, searched: object) -> str:
    """Show the actual airport flown, annotating the searched metro code when it
    differs. e.g. actual=FCO searched=ROM -> "FCO (ROM)"; equal/missing -> as-is.
    A searched value may be a comma list of ALTERNATIVES ("ASJ,SES"); when the
    actual airport is one of them it is not an annotation-worthy difference, so
    show the actual code plainly instead of "ASJ (ASJ,SES)".
    """
    actual_code = str(actual or "").strip().upper()
    searched_code = str(searched or "").strip().upper()
    if not actual_code:
        return searched_code
    if searched_code and actual_code in [c.strip() for c in searched_code.split(",")]:
        return actual_code
    if searched_code and searched_code != actual_code:
        return f"{actual_code} ({searched_code})"
    return actual_code


def _fare_airport_code(result, itinerary: dict) -> str:
    """The single airport this fare's data actually belongs to, shown plainly
    (e.g. "CDG" or "ORY") in the Airport column. Combined multi-airport groups
    save rows under "ORY,CDG", so without this column the client can't tell which
    airport the price is for. Prefers the actual outbound arrival airport the
    scraper extracted; falls back to the row's stored destination when that is a
    single airport (legacy/single-destination rows). N-A when unknowable (e.g. a
    combined row whose render didn't expose the airport)."""
    actual = str(itinerary.get("actual_outbound_destination") or "").strip().upper()
    if actual:
        return actual
    stored = str(getattr(result, "destination", "") or "").strip().upper()
    if stored and "," not in stored:
        return stored
    return _MISSING_VALUE


def _na_verification_link(
    template_link: str | None,
    depart_date: date,
    return_date: date | None,
) -> str:
    """A Kayak search link for an N-A (no-fare) row so the client can manually
    verify. We reuse a SIBLING result's stored deep_link as a template (it already
    carries the exact market base + sort/-MULT/flylocal/baditin/stops filters the
    scraper searched) and swap in this row's dates -- so the link is identical to
    what the scraper used, with no duplicated URL logic. Returns N-A if no template
    exists yet (no row on this route has collected a real link)."""
    if not template_link:
        return _MISSING_VALUE
    # The stable URL is .../flights/<ROUTE>/<DEPART>[/<RETURN>]?<filters>. Swap the
    # date segment(s) only; leave route + query untouched.
    dates = depart_date.isoformat()
    if return_date is not None:
        dates += f"/{return_date.isoformat()}"
    swapped = re.sub(
        r"(/flights/[^/]+/)\d{4}-\d{2}-\d{2}(?:/\d{4}-\d{2}-\d{2})?",
        lambda m: f"{m.group(1)}{dates}",
        template_link,
        count=1,
    )
    return swapped or template_link


def _searched_leg_pairs(route_group: RouteGroup, origin: str, destination: str) -> list[tuple[str, str]]:
    """The SEARCHED (origin, destination) code for each leg in order: leg 1 =
    origin->destination, then each configured extra leg (empty destination = home).
    Same order as the stored actual legs, so actual airports can be annotated with
    the searched metro code by position."""
    home = origin.upper()
    pairs: list[tuple[str, str]] = [(home, destination.upper())]
    for leg in (getattr(route_group, "multi_city_legs", None) or []):
        if not isinstance(leg, dict):
            continue
        leg_o = str(leg.get("origin") or "").upper()
        leg_d = str(leg.get("destination") or "").upper() or home
        if leg_o:
            pairs.append((leg_o, leg_d))
    return pairs


def _multi_city_route_label(
    itinerary: dict,
    dep_airport: str,
    arr_airport: str,
    return_from: str,
    config_route: str,
    searched_pairs: list[tuple[str, str]],
) -> str:
    """Each flight leg as a FROM-TO pair joined by ' / ', as the trip is flown/
    configured (open-jaw: pairs do NOT chain). Prefers the actual per-leg airports
    in itinerary['legs'], annotated with the searched metro code at the same
    position (e.g. NRT (TYO) / ICN (SEL)); else the dep/arrival + return-from
    endpoints; else the form-configured route (used for N-A rows).

    e.g. YVR-BER / BUD-YVR  (open-jaw, 2 flights)
         YVR-BER / BER-LON / BUD-YVR  (3 flights)
    """
    legs = itinerary.get("legs")
    pairs: list[str] = []
    if isinstance(legs, list):
        for i, leg in enumerate(legs):
            if not isinstance(leg, dict):
                continue
            searched = searched_pairs[i] if i < len(searched_pairs) else ("", "")
            o = _display_airport(leg.get("actual_origin"), searched[0])
            d = _display_airport(leg.get("actual_destination"), searched[1])
            if o and d:
                pairs.append(f"{o}-{d}")
    if pairs:
        return " / ".join(pairs)

    # No per-leg data (e.g. an N-A row): use the form-configured route so the cell
    # still shows the intended itinerary rather than a degenerate endpoint guess.
    if return_from:
        return f"{dep_airport}-{arr_airport} / {return_from}-{dep_airport}"
    return config_route


def _safe_stop_label(value: object, stops: object = None) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(stops, (int, float)):
        stop_count = int(stops)
        if stop_count <= 0:
            return "Direct"
        if stop_count == 1:
            return "1 Stop"
        return f"{stop_count} Stops"
    return ""


def _format_duration_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def _parse_duration_text(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    import re

    text = value.lower().replace("hours", "h").replace("hour", "h")
    text = text.replace("minutes", "m").replace("minute", "m").replace("mins", "m").replace("min", "m")
    hours_match = re.search(r"(\d+)\s*h", text)
    mins_match = re.search(r"(\d+)\s*m", text)
    hours = int(hours_match.group(1)) if hours_match else 0
    mins = int(mins_match.group(1)) if mins_match else 0
    total = hours * 60 + mins
    return total if total > 0 else None


def _duration_label_from_minutes(values: list[int]) -> str:
    return " / ".join(_format_duration_minutes(value) for value in values if value > 0)


def _safe_duration_label(result: AllFlightResult) -> str:
    itinerary = getattr(result, "itinerary_data", None)
    if isinstance(itinerary, dict):
        raw_durations = itinerary.get("leg_durations")
        if isinstance(raw_durations, list):
            durations = [int(value) for value in raw_durations if isinstance(value, (int, float)) and int(value) > 0]
            if durations:
                return _duration_label_from_minutes(durations)

        legs = itinerary.get("legs")
        if isinstance(legs, list):
            durations: list[int] = []
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                raw_minutes = leg.get("duration_minutes")
                if isinstance(raw_minutes, (int, float)) and int(raw_minutes) > 0:
                    durations.append(int(raw_minutes))
                    continue
                parsed = _parse_duration_text(leg.get("duration_text"))
                if parsed:
                    durations.append(parsed)
            if durations:
                return _duration_label_from_minutes(durations)

        parsed_parts = [
            _parse_duration_text(part)
            for part in str(itinerary.get("duration_text") or "").split("/")
        ]
        durations = [value for value in parsed_parts if value]
        if durations:
            return _duration_label_from_minutes(durations)

    duration = getattr(result, "duration_minutes", None)
    if isinstance(duration, int) and duration > 0:
        return _format_duration_minutes(duration)
    return ""


def _duration_rank(result: AllFlightResult) -> int:
    duration = getattr(result, "duration_minutes", None)
    return duration if isinstance(duration, int) and duration > 0 else 10**9


def _stops_rank(result: AllFlightResult) -> int:
    stops = getattr(result, "stops", None)
    return stops if isinstance(stops, int) and stops >= 0 else 10**9


def _result_sort_key(result: AllFlightResult) -> tuple[float, int, int]:
    return (float(result.price), _duration_rank(result), _stops_rank(result))


def _set_date_cell(ws, *, row: int, column: int, value: object):
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            pass
    cell = ws.cell(row=row, column=column, value=value)
    cell.number_format = "DD-MM-YYYY"
    return cell


def _safe_sheet_title(wb: Workbook, value: object, *, fallback: str = "Sheet") -> str:
    base = _INVALID_SHEET_TITLE_RE.sub("-", str(value or "").strip()).strip("' ")
    if not base:
        base = fallback
    base = base[:31]
    if base not in wb.sheetnames:
        return base

    suffix = 2
    while True:
        suffix_text = f"-{suffix}"
        candidate = f"{base[:31 - len(suffix_text)]}{suffix_text}"
        if candidate not in wb.sheetnames:
            return candidate
        suffix += 1


def _export_dates(route_group: RouteGroup, fallback_dates: list[date]) -> list[date]:
    unique_fallback = sorted({d for d in fallback_dates if isinstance(d, date)})
    configured_start = getattr(route_group, "start_date", None)
    configured_end = getattr(route_group, "end_date", None)
    raw_days_ahead = getattr(route_group, "days_ahead", None)

    configured_start = configured_start if isinstance(configured_start, date) else None
    configured_end = configured_end if isinstance(configured_end, date) else None
    days_ahead = max(1, min(raw_days_ahead, 730)) if isinstance(raw_days_ahead, int) else None

    if configured_start or configured_end:
        if configured_start is None:
            configured_start = unique_fallback[0] if unique_fallback else date.today()
        if configured_end is None:
            configured_end = configured_start + timedelta(days=(days_ahead or 1) - 1)
        if configured_end >= configured_start:
            total_days = min((configured_end - configured_start).days + 1, 730)
            return [configured_start + timedelta(days=i) for i in range(total_days)]

    return unique_fallback


def export_route_group(
    route_group: RouteGroup,
    all_results: list[AllFlightResult],
    *,
    include_links: bool = False,
) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    if not all_results:
        ws = wb.create_sheet("No Data")
        ws["A1"] = "No results available"
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        return output.read()

    if route_group.trip_type == "multi_city":
        return _export_multi_city_route_group(
            wb, route_group, all_results, include_links=include_links
        )

    # --------------------------------------------------
    # LOOKUPS
    # --------------------------------------------------

    all_dates = _export_dates(route_group, [r.depart_date for r in all_results])

    cheapest_by_origin_date: dict[tuple[str, object], AllFlightResult] = {}

    for r in all_results:
        key = (r.origin, r.depart_date)

        if key not in cheapest_by_origin_date:
            cheapest_by_origin_date[key] = r
        elif _result_sort_key(r) < _result_sort_key(cheapest_by_origin_date[key]):
            cheapest_by_origin_date[key] = r

    # --------------------------------------------------
    # MAIN ORIGIN SHEETS
    # --------------------------------------------------

    sheet_name_map = route_group.sheet_name_map or {
        o: o for o in route_group.origins
    }
    combined_origin = combined_origin_for_group(route_group)
    sheet_entries = (
        [
            (
                combined_origin,
                sheet_name_map.get(combined_origin)
                or ", ".join(str(origin).strip().upper() for origin in (route_group.origins or []))
                or combined_origin,
            )
        ]
        if combined_origin
        else list(sheet_name_map.items())
    )

    main_headers = list(_MAIN_HEADERS) + (["Verification Link"] if include_links else [])

    # Route column shows the full path in one cell: ORIGIN-DEST-ORIGIN for a round
    # trip (e.g. MAN-VCE-MAN). The destination uses the actual airport flown (e.g.
    # FCO when ROM metro was searched), falling back to the group's dest codes.
    group_dest_codes = ", ".join(
        str(code).strip().upper() for code in (route_group.destinations or []) if str(code).strip()
    )

    for origin, sheet_name in sheet_entries:
        ws = wb.create_sheet(title=_safe_sheet_title(wb, sheet_name, fallback=origin))
        _write_header_row(ws, main_headers)

        # A template search link for N-A rows = any collected result's deep_link for
        # this origin (the stable Kayak search URL with the exact scraper filters);
        # we swap this row's dates into it so even no-fare dates get a clickable
        # verify-link instead of N-A.
        na_template = next(
            (
                r.deep_link
                for (o, _d), r in cheapest_by_origin_date.items()
                if o == origin and getattr(r, "deep_link", None)
            ),
            None,
        )

        for row_idx, d in enumerate(all_dates, start=2):
            result = cheapest_by_origin_date.get((origin, d))

            arrival_airport = group_dest_codes
            if result is not None:
                itinerary = getattr(result, "itinerary_data", None)
                actual_dest = (itinerary or {}).get("actual_outbound_destination") if isinstance(itinerary, dict) else None
                resolved = _display_airport(actual_dest, getattr(result, "destination", "") or group_dest_codes)
                if resolved:
                    arrival_airport = resolved

            # Round trip route = out + back: MAN-VCE-MAN.
            route = f"{origin}-{arrival_airport or group_dest_codes}-{origin}"

            # Return Date = return flight date. Prefer the actual return_date the
            # scraper captured; else depart + nights (the round-trip return rule).
            ending_date = None
            if result is not None and isinstance(getattr(result, "itinerary_data", None), dict):
                ending_date = result.itinerary_data.get("return_date")
            if not ending_date:
                ending_date = d + timedelta(days=int(route_group.nights or 0))

            _set_date_cell(ws, row=row_idx, column=1, value=d)
            _set_date_cell(ws, row=row_idx, column=2, value=ending_date)
            ws.cell(row=row_idx, column=3, value=route)
            ws.cell(row=row_idx, column=5, value=route_group.nights)

            if result:
                itinerary_data = itinerary if isinstance(itinerary, dict) else {}
                ws.cell(row=row_idx, column=4, value=_fare_airport_code(result, itinerary_data))
                ws.cell(row=row_idx, column=6, value=result.airline)
                ws.cell(
                    row=row_idx,
                    column=7,
                    value=_safe_stop_label(result.stop_label, result.stops),
                )
                ws.cell(
                    row=row_idx,
                    column=8,
                    value=_safe_duration_label(result),
                )
                ws.cell(
                    row=row_idx,
                    column=9,
                    value=int(round(float(result.price))),
                )
                if include_links:
                    ws.cell(row=row_idx, column=10, value=result.deep_link or _MISSING_VALUE)
            else:
                ws.cell(row=row_idx, column=4, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=6, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=7, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=8, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=9, value=_MISSING_VALUE)
                if include_links:
                    # No fare found, but still give a clickable search link to verify.
                    ws.cell(
                        row=row_idx,
                        column=10,
                        value=_na_verification_link(na_template, d, ending_date),
                    )

        _autosize_columns(ws)

    # --------------------------------------------------
    # SPECIAL JOURNEY SHEETS  (additional return / multi-city legs the
    # operator added in the "Advanced Routes" form)
    # --------------------------------------------------

    for sheet in route_group.special_sheets or []:
        sheet_name = sheet.get("name") or "Journey"
        sheet_origin = (sheet.get("origin") or "").upper()
        sheet_dest_label = sheet.get("destination_label") or sheet_origin
        sheet_dests = [d.upper() for d in (sheet.get("destinations") or [])]
        columns = int(sheet.get("columns") or 4)

        ws = wb.create_sheet(title=_safe_sheet_title(wb, sheet_name, fallback="Journey"))

        if columns >= 6:
            # Special sheets keep their own layout (no Return Date column) so the
            # main-sheet Return Date addition doesn't shift their cells.
            _write_header_row(
                ws,
                ["Date", "Dep Airport", "Arrival Airport", "Nights", "Airline", "Stop Result", "Duration", "Flight Price"],
            )
        else:
            _write_header_row(
                ws, ["Date", "Dep Airport", "Arrival Airport", "Flight Price"]
            )

        # cheapest result per date across this special sheet's destinations
        cheapest_per_date: dict[object, AllFlightResult] = {}
        for r in all_results:
            if r.origin != sheet_origin or r.destination not in sheet_dests:
                continue
            if (
                r.depart_date not in cheapest_per_date
                or _result_sort_key(r) < _result_sort_key(cheapest_per_date[r.depart_date])
            ):
                cheapest_per_date[r.depart_date] = r

        for row_idx, d in enumerate(all_dates, start=2):
            result = cheapest_per_date.get(d)

            _set_date_cell(ws, row=row_idx, column=1, value=d)
            ws.cell(row=row_idx, column=2, value=sheet_origin)
            ws.cell(row=row_idx, column=3, value=sheet_dest_label)

            if columns >= 6:
                ws.cell(row=row_idx, column=4, value=route_group.nights)
                if result:
                    ws.cell(row=row_idx, column=5, value=result.airline)
                    ws.cell(row=row_idx, column=6, value=_safe_stop_label(result.stop_label, result.stops))
                    ws.cell(row=row_idx, column=7, value=_safe_duration_label(result))
                    ws.cell(
                        row=row_idx,
                        column=8,
                        value=int(round(float(result.price))),
                    )
                else:
                    ws.cell(row=row_idx, column=5, value=_MISSING_VALUE)
                    ws.cell(row=row_idx, column=6, value=_MISSING_VALUE)
                    ws.cell(row=row_idx, column=7, value=_MISSING_VALUE)
                    ws.cell(row=row_idx, column=8, value=_MISSING_VALUE)
            else:
                if result:
                    ws.cell(
                        row=row_idx,
                        column=4,
                        value=int(round(float(result.price))),
                    )
                else:
                    ws.cell(row=row_idx, column=4, value=_MISSING_VALUE)

        _autosize_columns(ws)

    # --------------------------------------------------
    # FINISH
    # --------------------------------------------------

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return output.read()


def _export_multi_city_route_group(
    wb: Workbook,
    route_group: RouteGroup,
    all_results: list[AllFlightResult],
    *,
    include_links: bool = False,
) -> bytes:
    itinerary_rows = [r for r in all_results if r.itinerary_data]
    if not itinerary_rows:
        ws = wb.create_sheet("No Data")
        ws["A1"] = "No itinerary results available"
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        return output.read()

    sheet_name_map = route_group.sheet_name_map or {o: o for o in route_group.origins}
    all_dates = _export_dates(route_group, [row.depart_date for row in itinerary_rows])

    cheapest_by_origin_date: dict[tuple[str, object], AllFlightResult] = {}
    for row in itinerary_rows:
        key = (row.origin, row.depart_date)
        current = cheapest_by_origin_date.get(key)
        if current is None or _result_sort_key(row) < _result_sort_key(current):
            cheapest_by_origin_date[key] = row

    rows_by_origin: dict[str, list[AllFlightResult]] = {}
    for row in cheapest_by_origin_date.values():
        rows_by_origin.setdefault(row.origin, []).append(row)

    itinerary_prices_by_origin: dict[str, list[float]] = {}
    all_itinerary_prices: list[AllFlightResult] = []

    headers = list(_MULTI_CITY_HEADERS)
    if include_links:
        headers = headers + ["Verification Link"]

    raw_group_destinations = getattr(route_group, "destinations", None)
    if raw_group_destinations is None:
        raw_group_destinations = [row.destination for row in itinerary_rows]
    configured_destinations = [
        str(destination).strip().upper()
        for destination in (raw_group_destinations or [])
        if str(destination).strip()
    ]
    fallback_destination = ",".join(configured_destinations)

    for origin, rows in sorted(rows_by_origin.items()):
        sheet_name = sheet_name_map.get(origin, origin)
        ws = wb.create_sheet(title=_safe_sheet_title(wb, sheet_name, fallback=origin))
        _write_header_row(ws, headers)

        rows_by_date = {row.depart_date: row for row in rows}
        # Template link for N-A rows: any collected deep_link for this origin.
        na_template = next(
            (r.deep_link for r in rows if getattr(r, "deep_link", None)), None
        )

        for row_idx, depart_date in enumerate(all_dates, start=2):
            result = rows_by_date.get(depart_date)
            itinerary = result.itinerary_data if isinstance(result, object) and result else {}
            if not isinstance(itinerary, dict):
                itinerary = {}
            searched_destination = result.destination if result else fallback_destination
            return_date = itinerary.get("return_date")
            # Prefer the ACTUAL airport flown (e.g. FCO when the group searched the
            # ROM metro code), falling back to the searched code. _display_airport
            # appends the searched metro code in brackets when they differ, e.g.
            # "FCO (ROM)", so the sheet shows the real airport without losing context.
            dep_airport = _display_airport(itinerary.get("actual_outbound_origin"), origin)
            arr_airport = _display_airport(
                itinerary.get("actual_outbound_destination"),
                searched_destination,
            )
            return_from = _display_airport(
                itinerary.get("actual_return_origin"),
                itinerary.get("return_origin") or (itinerary.get("inbound") or {}).get("origin"),
            )

            # Route column = each ACTUAL flight leg as a FROM-TO pair joined by " / ",
            # exactly as the trip is flown/configured (open-jaw: the pairs do NOT
            # chain -- the gap between e.g. BER and BUD is the open jaw, no flight).
            # e.g. YVR-BER / BUD-YVR, or YVR-BER / BER-LON / BUD-YVR. Built from the
            # per-leg airports when available, else the dep/arrival/return-from
            # endpoints. _route_legs returns the actual-airport pairs (metro-annotated).
            searched_pairs = _searched_leg_pairs(route_group, origin, searched_destination)
            config_route = " / ".join(f"{o}-{d}" for o, d in searched_pairs)
            route = _multi_city_route_label(
                itinerary,
                dep_airport or origin,
                arr_airport or searched_destination,
                return_from,
                config_route,
                searched_pairs,
            )

            _set_date_cell(ws, row=row_idx, column=1, value=depart_date)
            if return_date:
                _set_date_cell(ws, row=row_idx, column=2, value=return_date)
            else:
                ws.cell(row=row_idx, column=2, value=_MISSING_VALUE)
            ws.cell(row=row_idx, column=3, value=route)
            ws.cell(row=row_idx, column=5, value=route_group.nights)
            if result:
                ws.cell(row=row_idx, column=4, value=_fare_airport_code(result, itinerary))
                ws.cell(row=row_idx, column=6, value=result.airline)
                ws.cell(row=row_idx, column=7, value=_safe_stop_label(result.stop_label, result.stops))
                ws.cell(row=row_idx, column=8, value=_safe_duration_label(result))
                ws.cell(row=row_idx, column=9, value=int(round(float(result.price))))
                if include_links:
                    ws.cell(row=row_idx, column=10, value=result.deep_link or _MISSING_VALUE)
                itinerary_prices_by_origin.setdefault(origin, []).append(float(result.price))
                all_itinerary_prices.append(result)
            else:
                ws.cell(row=row_idx, column=4, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=6, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=7, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=8, value=_MISSING_VALUE)
                ws.cell(row=row_idx, column=9, value=_MISSING_VALUE)
                if include_links:
                    # No fare, but still give a clickable verify link. Multi-city
                    # chain URLs have several date segments; swap only the leading
                    # depart date, leaving the rest of the (route + filters) intact.
                    ws.cell(
                        row=row_idx,
                        column=10,
                        value=_na_verification_link(na_template, depart_date, None),
                    )

        _autosize_columns(ws)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output.read()


def _write_header_row(ws, headers: list[str]) -> None:
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")


def _autosize_columns(ws) -> None:
    # Center every cell (data + headers) horizontally and vertically. Called once
    # per sheet on both export paths, so this aligns the whole workbook uniformly.
    center = Alignment(horizontal="center", vertical="center")
    for col_cells in ws.columns:
        max_length = max(
            (
                len(str(c.value))
                for c in col_cells
                if c.value is not None
            ),
            default=0,
        )

        for c in col_cells:
            # Preserve the bold header font set in _write_header_row; only the
            # alignment is (re)applied here.
            c.alignment = center

        col_letter = get_column_letter(col_cells[0].column)
        ws.column_dimensions[col_letter].width = max_length + 3
