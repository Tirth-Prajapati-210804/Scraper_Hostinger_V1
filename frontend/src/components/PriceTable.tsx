import { useMemo, useState } from "react";

import type { DailyPrice } from "../types/price";
import type { MultiCityLegConfig, TripType } from "../types/route-group";
import { formatDisplayDate, formatFreshnessLabel } from "../utils/format";
import { Button } from "./ui/Button";
import { Skeleton } from "./ui/Skeleton";

// "route" and "airport" are synthetic columns with no DailyPrice field of their
// own, so sorting falls back to origin / destination respectively.
type SortableKey = keyof DailyPrice;
interface Column {
  key: SortableKey | "route" | "airport";
  label: string;
  align?: "left" | "right";
}

const BASE_COLUMNS: Column[] = [
  { key: "depart_date", label: "Date" },
  { key: "route", label: "Route" },
  { key: "airport", label: "Airport" },
  { key: "airline", label: "Airline" },
  { key: "stops", label: "Stops" },
  { key: "duration_minutes", label: "Duration" },
  { key: "price", label: "Price", align: "right" },
  { key: "deep_link", label: "Link" },
  { key: "scraped_at", label: "Freshness" },
];

// The single airport this fare's data belongs to (e.g. "CDG" vs "ORY").
// Combined multi-airport groups store destination "ORY,CDG", so the client
// can't tell from the Route alone which airport the price is for. Prefers the
// actual arrival airport the scraper extracted; falls back to the stored
// destination when it is one plain airport code.
function fareAirport(price: DailyPrice): string {
  const actual = (
    price.itinerary_data?.legs?.[0]?.actual_destination ??
    price.itinerary_data?.actual_outbound_destination ??
    ""
  ).trim().toUpperCase();
  if (actual) return actual;
  const stored = (price.destination ?? "").trim().toUpperCase();
  if (stored && !stored.includes(",")) return stored;
  return "-";
}

// Show the actual airport flown, annotating the searched code when they differ:
// actual=NRT searched=TYO -> "NRT (TYO)"; equal/missing -> as-is. Mirrors the
// export's _display_airport so the table and Excel read the same.
function displayAirport(actual?: string | null, searched?: string | null): string {
  const a = (actual ?? "").trim().toUpperCase();
  const s = (searched ?? "").trim().toUpperCase();
  if (!a) return s;
  if (s && s.split(",").map((code) => code.trim()).includes(a)) return a;
  if (s && s !== a) return `${a} (${s})`;
  return a;
}

// Round trip = compact chain MAN-VCE-MAN (with metro annotation, e.g.
// YVR-FCO (ROM)-YVR).
// Multi-city (open-jaw) = each flight leg as a FROM-TO pair joined by " / ", e.g.
// YVR-BER / BUD-YVR or YVR-BER / BER-LON / BUD-YVR. The pairs deliberately do NOT
// chain (the gap = the open jaw). Prefers the ACTUAL airports flown (from
// itinerary_data.legs, so NRT (TYO) shows like the export); falls back to the
// group's form leg config when no per-leg data is present (e.g. N-A rows).
function buildRoute(
  price: DailyPrice,
  isMultiCity: boolean,
  homeOrigin: string,
  legs?: MultiCityLegConfig[] | null,
): string {
  const actualLegs = price.itinerary_data?.legs;

  if (!isMultiCity) {
    const firstLeg = actualLegs?.[0];
    const lastLeg = actualLegs?.[actualLegs.length - 1];
    const origin = displayAirport(
      firstLeg?.actual_origin ?? price.itinerary_data?.actual_outbound_origin,
      price.origin,
    );
    const dest = displayAirport(
      firstLeg?.actual_destination ?? price.itinerary_data?.actual_outbound_destination,
      price.destination,
    );
    const returnTo = displayAirport(
      lastLeg?.actual_destination ?? price.itinerary_data?.actual_return_destination,
      price.origin,
    );
    return `${origin}-${dest}-${returnTo}`;
  }

  // The SEARCHED leg codes in order: leg 1 = origin->destination, then each
  // configured extra leg (empty destination = back to home). Same order as the
  // stored actual legs, so we can annotate each actual airport with its searched
  // metro code by position -> e.g. NRT (TYO) / ICN (SEL).
  const searchedPairs: Array<[string, string]> = [
    [price.origin.toUpperCase(), price.destination.toUpperCase()],
    ...(legs ?? []).map(
      (leg) =>
        [leg.origin.toUpperCase(), (leg.destination || homeOrigin).toUpperCase()] as [string, string],
    ),
  ];

  // Prefer the real per-leg airports (matches the export), annotated with the
  // searched code at the same position when they differ.
  if (actualLegs && actualLegs.length > 0) {
    const pairs = actualLegs
      .map((leg, i) => {
        const searched = searchedPairs[i];
        const o = displayAirport(leg.actual_origin, searched?.[0]);
        const d = displayAirport(leg.actual_destination, searched?.[1]);
        return o && d ? `${o}-${d}` : "";
      })
      .filter(Boolean);
    if (pairs.length > 0) return pairs.join(" / ");
  }

  // Fallback (no per-leg data, e.g. N-A rows): the searched codes from the form.
  return searchedPairs.map(([o, d]) => `${o}-${d}`).join(" / ");
}

interface PriceTableProps {
  prices: DailyPrice[];
  isLoading: boolean;
  hasMore?: boolean;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  groupCurrency?: string;
  tripType?: TripType;
  nights?: number;
  returnOrigin?: string | null;
  /** Home origin + configured extra legs, used to build the multi-city Route
   *  column exactly as entered in the form. */
  homeOrigin?: string;
  multiCityLegs?: MultiCityLegConfig[] | null;
}

function addDays(rawDate: string, days: number): string {
  const [year, month, day] = rawDate.split("-").map(Number);
  const value = new Date(Date.UTC(year, (month ?? 1) - 1, day ?? 1));
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
}

function formatStopResult(price: DailyPrice): { label: string; tone: string } {
  const explicit = price.stop_label?.trim();
  if (explicit) {
    if (explicit.toLowerCase().includes("direct")) {
      return { label: explicit, tone: "text-green-600" };
    }
    if (explicit.toLowerCase().includes("unavailable")) {
      return { label: explicit, tone: "text-amber-600" };
    }
    return { label: explicit, tone: "text-slate-700" };
  }

  if (price.stops == null) {
    return { label: "-", tone: "text-slate-500" };
  }
  if (price.stops === 0) {
    return { label: "Direct", tone: "text-green-600" };
  }
  return {
    label: `${price.stops} stop${price.stops > 1 ? "s" : ""}`,
    tone: "text-slate-700",
  };
}

function formatDurationMinutes(minutes: number): string {
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatDuration(price: DailyPrice): string {
  const itinerary = price.itinerary_data;
  const legDurations = itinerary?.leg_durations?.filter((value) => Number.isFinite(value) && value > 0);
  if (legDurations?.length) {
    return legDurations.map(formatDurationMinutes).join(" / ");
  }

  const legText = itinerary?.legs
    ?.map((leg) => {
      if (leg.duration_minutes && leg.duration_minutes > 0) return formatDurationMinutes(leg.duration_minutes);
      return leg.duration_text?.trim() || "";
    })
    .filter(Boolean);
  if (legText?.length) {
    return legText.join(" / ");
  }

  const explicit = itinerary?.duration_text?.trim();
  if (explicit?.includes("/")) return explicit;

  return price.duration_minutes == null ? "-" : formatDurationMinutes(price.duration_minutes);
}

function HeaderCell({
  column,
  sortDir,
  sortKey,
  onToggleSort,
}: {
  column: Column;
  sortDir: "asc" | "desc";
  sortKey: keyof DailyPrice;
  onToggleSort: (key: Column["key"]) => void;
}) {
  const isSorted = sortKey === column.key;
  return (
    <th
      className={`cursor-pointer select-none px-6 py-3 hover:text-slate-700 ${
        column.align === "right" ? "text-right" : ""
      }`}
      onClick={() => onToggleSort(column.key)}
    >
      {column.label} {isSorted ? (sortDir === "asc" ? "↑" : "↓") : ""}
    </th>
  );
}

function FragmentWithMultiCityHeaders({
  column,
  isMultiCity,
  sortDir,
  sortKey,
  onToggleSort,
}: {
  column: Column;
  isMultiCity: boolean;
  sortDir: "asc" | "desc";
  sortKey: keyof DailyPrice;
  onToggleSort: (key: Column["key"]) => void;
}) {
  return (
    <>
      <HeaderCell
        column={column}
        sortDir={sortDir}
        sortKey={sortKey}
        onToggleSort={onToggleSort}
      />
      {/* Multi-city: a Return Date column follows the single Route column. */}
      {isMultiCity && column.key === "route" ? <th className="px-6 py-3">Return Date</th> : null}
    </>
  );
}

export function PriceTable({
  prices,
  isLoading,
  hasMore,
  onLoadMore,
  loadingMore,
  groupCurrency,
  tripType,
  nights = 0,
  homeOrigin,
  multiCityLegs,
}: PriceTableProps) {
  const [sortKey, setSortKey] = useState<keyof DailyPrice>("depart_date");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const isMultiCity = tripType === "multi_city";

  const columns = useMemo(() => {
    if (!isMultiCity) {
      return BASE_COLUMNS;
    }

    // Multi-city: the destination column is rendered as Return From + Return Date
    // (see FragmentWithMultiCityHeaders), so only the price relabel applies here.
    return BASE_COLUMNS.map((column) => {
      if (column.key === "price") {
        return { ...column, label: "Total Fare" };
      }
      return column;
    });
  }, [isMultiCity]);

  // Synthetic columns have no DailyPrice field; sort route by origin and
  // airport by the stored destination.
  function toggleSort(key: Column["key"]) {
    const sortable: keyof DailyPrice =
      key === "route" ? "origin" : key === "airport" ? "destination" : key;
    if (sortKey === sortable) {
      setSortDir((dir) => (dir === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(sortable);
    setSortDir("asc");
  }

  const sorted = useMemo(() => {
    return [...prices].sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];

      if (av == null) return 1;
      if (bv == null) return -1;

      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [prices, sortDir, sortKey]);

  if (isLoading) {
    return <Skeleton className="h-64 rounded-xl" />;
  }

  if (!prices.length) {
    return (
      <p className="py-10 text-center text-sm text-slate-400">
        No prices found. Run a collection to populate data.
      </p>
    );
  }

  return (
    <>
      <div className="block w-full max-w-full overflow-x-auto overscroll-x-contain pb-1">
        <table className="w-max min-w-full text-left text-sm">
          <thead>
            <tr className="border-y border-slate-200 bg-slate-50 text-xs uppercase tracking-wider text-slate-500">
              {columns.map((col) => (
                <FragmentWithMultiCityHeaders
                  key={col.key}
                  column={col}
                  isMultiCity={isMultiCity}
                  sortDir={sortDir}
                  sortKey={sortKey}
                  onToggleSort={toggleSort}
                />
              ))}
            </tr>
          </thead>

          <tbody>
            {sorted.map((price, i) => {
              const stopResult = formatStopResult(price);
              return (
                <tr
                  key={price.id}
                  className={`transition-colors hover:bg-brand-50/40 ${
                    i % 2 !== 0 ? "bg-slate-50/50" : ""
                  }`}
                >
                  <td className="whitespace-nowrap px-6 py-3 text-slate-700">{formatDisplayDate(price.depart_date)}</td>
                  {/* Single Route column: MAN-VCE-MAN (round trip) or the full
                      multi-city loop YEG-NRT-ICN-YEG. */}
                  <td className="whitespace-nowrap px-6 py-3 font-medium text-slate-800">
                    <span className="rounded-md bg-brand-50 px-2 py-1 font-mono text-xs font-semibold text-brand-700">
                      {buildRoute(price, isMultiCity, homeOrigin ?? price.origin, multiCityLegs)}
                    </span>
                  </td>
                  {isMultiCity ? (
                    <td className="whitespace-nowrap px-6 py-3 text-slate-700">
                      {formatDisplayDate(addDays(price.depart_date, nights))}
                    </td>
                  ) : null}
                  <td className="whitespace-nowrap px-6 py-3 text-slate-700">
                    {price._missing ? (
                      <span className="text-slate-300">-</span>
                    ) : (
                      <span className="rounded-md bg-emerald-50 px-2 py-1 font-mono text-xs font-semibold text-emerald-700">
                        {fareAirport(price)}
                      </span>
                    )}
                  </td>
                  <td className="min-w-[16rem] px-6 py-3 text-slate-700">
                    {price._missing ? <span className="text-slate-300">-</span> : price.airline}
                  </td>
                  <td className="whitespace-nowrap px-6 py-3 text-slate-700">
                    {price._missing ? (
                      <span className="text-slate-300">-</span>
                    ) : (
                      <span className={`font-medium ${stopResult.tone}`}>{stopResult.label}</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-6 py-3 text-slate-700">
                    {price._missing ? <span className="text-slate-300">-</span> : formatDuration(price)}
                  </td>
                  <td className="whitespace-nowrap px-6 py-3 text-right font-medium text-slate-900">
                    {price._missing ? (
                      <span className="text-slate-300">-</span>
                    ) : (
                      <>
                        {Math.round(price.price).toLocaleString()}{" "}
                        <span className="text-xs text-slate-400">{groupCurrency ?? price.currency}</span>
                      </>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-6 py-3 text-slate-500">
                    {price.deep_link ? (
                      <a
                        href={price.deep_link}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-medium text-brand-600 underline-offset-2 hover:underline"
                      >
                        Open
                      </a>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-6 py-3 text-slate-400">
                    {price._missing ? (
                      <span className="text-slate-300">-</span>
                    ) : (
                      <div className="font-medium text-slate-600">{formatFreshnessLabel(price.scraped_at)}</div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {hasMore ? (
        <div className="border-t border-slate-100 px-6 py-4">
          <Button variant="secondary" onClick={onLoadMore} loading={loadingMore}>
            Load more
          </Button>
        </div>
      ) : null}
    </>
  );
}
