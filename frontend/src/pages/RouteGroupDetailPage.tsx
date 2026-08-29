import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { resetGroupCaps, triggerGroupCollection } from "../api/collection";
import { getErrorMessage } from "../api/client";
import { fetchPrices, fetchPriceTrend } from "../api/prices";
import {
  deleteRouteGroup,
  downloadExport,
  getRouteGroup,
  getRouteGroupProgress,
  saveBlobAsFile,
} from "../api/route-groups";
import { DateCoverageGrid } from "../components/DateCoverageGrid";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { ScrapeHealthPanel } from "../components/ScrapeHealthPanel";
// Lazy: PriceChart pulls in Recharts (~250KB). Load it only when the chart
// section actually renders, so the route-group page paints fast.
const PriceChart = lazy(() =>
  import("../components/PriceChart").then((m) => ({ default: m.PriceChart })),
);
import { PriceTable } from "../components/PriceTable";
import { RouteGroupForm } from "../components/RouteGroupForm";
import { Button } from "../components/ui/Button";
import { Select } from "../components/ui/Select";
import { Skeleton } from "../components/ui/Skeleton";
import { Badge, Btn, Card, Icon, IconBtn, MiniStat } from "../components/ds";
import { useToast } from "../context/ToastContext";
import type { DailyPrice } from "../types/price";
import { formatStopModeLabel } from "../utils/stopModes";
import { formatFreshnessLabel } from "../utils/format";
import { usePageTitle } from "../utils/usePageTitle";

/** Every ISO date from start..end inclusive (capped at 730 to bound the table). */
function enumerateDates(start: string | null, end: string | null): string[] {
  if (!start || !end) return [];
  const out: string[] = [];
  let cur = start;
  for (let i = 0; i < 730 && cur <= end; i++) {
    out.push(cur);
    cur = addDaysIso(cur, 1);
  }
  return out;
}

function addDaysIso(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, (m ?? 1) - 1, d ?? 1));
  dt.setUTCDate(dt.getUTCDate() + days);
  return dt.toISOString().slice(0, 10);
}

function combinedCode(values: string[]): string {
  const seen: string[] = [];
  values.forEach((value) => {
    const code = value.trim().toUpperCase();
    if (code && !seen.includes(code)) {
      seen.push(code);
    }
  });
  return seen.join(",");
}

/** Reuse a collected fare's stable search URL for a missing date by swapping the
 *  date segment(s). Same approach as the Excel export so links stay consistent. */
function swapSearchLinkDate(template: string | null, depart: string, ret: string): string | null {
  if (!template) return null;
  const dates = `${depart}/${ret}`;
  const swapped = template.replace(
    /(\/flights\/[^/]+\/)\d{4}-\d{2}-\d{2}(?:\/\d{4}-\d{2}-\d{2})?/,
    `$1${dates}`,
  );
  return swapped || template;
}

export function RouteGroupDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { showToast } = useToast();

  const [editOpen, setEditOpen] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [includeLinks, setIncludeLinks] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [confirmTrigger, setConfirmTrigger] = useState(false);
  const [resettingCaps, setResettingCaps] = useState(false);
  const [confirmResetCaps, setConfirmResetCaps] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [selectedOrigin, setSelectedOrigin] = useState<string>("");
  const [allPrices, setAllPrices] = useState<DailyPrice[]>([]);
  const [pricesLoading, setPricesLoading] = useState(false);
  const [priceHasMore, setPriceHasMore] = useState(false);
  const priceOffsetRef = useRef(0);
  const PRICE_PAGE = 100;

  const groupQuery = useQuery({
    queryKey: ["route-group", id],
    queryFn: () => getRouteGroup(id!),
    enabled: !!id,
  });

  const progressQuery = useQuery({
    queryKey: ["route-group-progress", id],
    queryFn: () => getRouteGroupProgress(id!),
    enabled: !!id,
    refetchInterval: 10_000,
  });

  const group = groupQuery.data;
  const combinedAlternativeOrigin = group && group.origins.length > 1 ? combinedCode(group.origins) : "";
  const originOptions = useMemo(
    () => (combinedAlternativeOrigin ? [combinedAlternativeOrigin] : (group?.origins ?? [])),
    [combinedAlternativeOrigin, group?.origins],
  );
  const activeOrigin = selectedOrigin || originOptions[0] || "";
  const originForQuery = activeOrigin;
  // Multi-airport destinations query the trend with the comma-joined key. Round
  // trip rows are saved under that literal combined destination key ("ORY,CDG").
  // Multi-city rows save the winning destination airport per date, so the
  // backend splits the comma key and matches any of the airports.
  const destForQuery =
    group && group.destinations.length > 1
      ? group.destinations.map((d) => d.trim().toUpperCase()).filter(Boolean).join(",")
      : group?.destinations[0] || "";
  const chainLegs = group?.trip_type === "multi_city" ? (group.multi_city_legs ?? null) : null;
  const returnOrigin =
    group?.trip_type === "multi_city"
      ? chainLegs?.length
        ? chainLegs[chainLegs.length - 1].origin
        : (group.special_sheets[0]?.origin ?? null)
      : null;
  // Days from depart to the FINAL homebound flight: leg nights are EXACT day
  // offsets (nights between legs), so the total shift is their sum. PriceTable
  // renders return date as depart + nights.
  const effectiveNights = chainLegs?.length
    ? chainLegs.reduce((days, leg) => days + leg.nights_before, 0)
    : (group?.nights ?? 0);

  // Show EVERY date in the travel window as a table row: collected fares as-is,
  // and missing dates as placeholder rows (Date/Route/Link populated, fare columns
  // "-"). The verify link reuses a collected row's deep_link with this date swapped
  // in -- same as the Excel export. Only fully fills once all pages are loaded so a
  // gap isn't shown for a date that lives on a later page.
  const filledPrices = useMemo<DailyPrice[]>(() => {
    // Only fill the per-date calendar when a single origin is selected; "All
    // origins" (selectedOrigin === "") mixes routes, so gap-filling is ambiguous.
    if (!group || !selectedOrigin || !activeOrigin) return allPrices;
    const windowDates = enumerateDates(group.start_date, group.end_date);
    if (!windowDates.length || priceHasMore) return allPrices;

    const byDate = new Map(allPrices.map((p) => [p.depart_date, p]));
    const template = allPrices.find((p) => p.deep_link)?.deep_link ?? null;
    const dest = destForQuery || group.destinations[0] || "";

    return windowDates.map((d) => {
      const existing = byDate.get(d);
      if (existing) return existing;
      return {
        id: `missing-${activeOrigin}-${d}`,
        origin: activeOrigin,
        destination: dest,
        depart_date: d,
        airline: "",
        price: NaN,
        currency: group.currency,
        provider: "",
        deep_link: swapSearchLinkDate(template, d, addDaysIso(d, effectiveNights)),
        stops: null,
        stop_label: null,
        duration_minutes: null,
        scraped_at: "",
        _missing: true,
      } satisfies DailyPrice;
    });
  }, [group, selectedOrigin, activeOrigin, allPrices, priceHasMore, effectiveNights, destForQuery]);

  const trendQuery = useQuery({
    queryKey: ["price-trend", id, originForQuery, destForQuery],
    queryFn: () =>
      fetchPriceTrend({ origin: originForQuery, destination: destForQuery, route_group_id: id }),
    enabled: !!originForQuery && !!destForQuery,
  });

  const loadPrices = useCallback(
    async (origin: string, newOffset: number) => {
      if (!id) return;
      setPricesLoading(true);
      try {
        const data = await fetchPrices({
          route_group_id: id,
          origin: origin || undefined,
          limit: PRICE_PAGE,
          offset: newOffset,
        });
        setAllPrices((prev) => (newOffset === 0 ? data : [...prev, ...data]));
        setPriceHasMore(data.length === PRICE_PAGE);
        priceOffsetRef.current = newOffset;
      } finally {
        setPricesLoading(false);
      }
    },
    [id],
  );

  const priceOriginRef = useRef("");

  useEffect(() => {
    if (!originOptions.length) return;
    if (!selectedOrigin || !originOptions.includes(selectedOrigin)) {
      setSelectedOrigin(originOptions[0]);
    }
  }, [originOptions, selectedOrigin]);

  useEffect(() => {
    if (!id || !originOptions.length || !activeOrigin) return;
    if (priceOriginRef.current === activeOrigin) return;

    priceOriginRef.current = activeOrigin;
    setAllPrices([]);
    void loadPrices(activeOrigin, 0);
  }, [activeOrigin, originOptions, id, loadPrices]);

  const handlePriceLoadMore = useCallback(
    () => loadPrices(activeOrigin, priceOffsetRef.current + PRICE_PAGE),
    [activeOrigin, loadPrices],
  );

  usePageTitle(group?.name ?? "Route Group");

  async function handleDownload() {
    if (!group) return;
    setDownloading(true);
    try {
      const blob = await downloadExport(group.id, includeLinks);
      saveBlobAsFile(blob, `${group.name.replace(/[^a-z0-9_-]/gi, "_")}.xlsx`);
      showToast("Excel downloaded", "success");
      setDownloadOpen(false);
    } catch (err) {
      showToast(getErrorMessage(err, "Download failed"), "error");
    } finally {
      setDownloading(false);
    }
  }

  async function handleTrigger() {
    if (!id) return;
    setConfirmTrigger(false);
    setTriggering(true);
    try {
      await triggerGroupCollection(id);
      showToast("Collection triggered successfully", "success");
      qc.invalidateQueries({ queryKey: ["collection-status"] });
      qc.invalidateQueries({ queryKey: ["route-group-progress", id] });
    } catch (err) {
      showToast(getErrorMessage(err, "Failed to trigger collection"), "error");
    } finally {
      setTriggering(false);
    }
  }

  async function handleResetCaps() {
    if (!id) return;
    setConfirmResetCaps(false);
    setResettingCaps(true);
    try {
      const result = await resetGroupCaps(id);
      showToast(
        `Retry caps reset — ${result.rows_cleared} skipped attempt(s) cleared. Trigger a scrape to collect them.`,
        "success",
      );
      qc.invalidateQueries({ queryKey: ["route-group-progress", id] });
    } catch (err) {
      showToast(getErrorMessage(err, "Failed to reset retry caps"), "error");
    } finally {
      setResettingCaps(false);
    }
  }

  if (groupQuery.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-48 rounded-xl" />
      </div>
    );
  }

  if (!group) {
    return (
      <div className="py-16 text-center text-slate-400">
        Route group not found.{" "}
        <Link to="/" className="text-brand-600 hover:underline">
          Back to dashboard
        </Link>
      </div>
    );
  }

  return (
    <ErrorBoundary>
      <div className="relative box-border w-full min-w-0 max-w-full stack-6" style={{ overflowX: "hidden" }}>
        <div className="ds-row ds-between ds-wrap ds-gap-3" style={{ minWidth: 0, overflowX: "hidden" }}>
          <Link
            to="/"
            className="ds-row ds-gap-2"
            style={{ fontSize: 13, fontWeight: 600, color: "var(--accent-600)" }}
          >
            <Icon name="back" size={15} />
            Back to Dashboard
          </Link>
          <div className="ds-row ds-wrap ds-gap-2" style={{ minWidth: 0, justifyContent: "flex-end" }}>
            <Btn variant="secondary" size="sm" icon="pencil" onClick={() => setEditOpen(true)}>
              Edit
            </Btn>
            <Btn variant="secondary" size="sm" icon="refresh" onClick={() => setConfirmTrigger(true)} loading={triggering}>
              Trigger scrape
            </Btn>
            <Btn
              variant="secondary"
              size="sm"
              icon="refresh"
              onClick={() => setConfirmResetCaps(true)}
              loading={resettingCaps}
            >
              Reset retry caps
            </Btn>
            <Btn variant="primary" size="sm" icon="download" onClick={() => setDownloadOpen(true)}>
              Download Excel
            </Btn>
            <IconBtn
              icon="trash"
              title="Delete route group"
              onClick={() => setConfirmDelete(true)}
              style={{ color: "var(--danger)", borderColor: "var(--danger-bd)", background: "var(--danger-bg)" }}
            />
          </div>
        </div>

        <Card className="w-full min-w-0 max-w-full overflow-hidden">
          <div className="ds-row ds-between ds-start ds-wrap ds-gap-4">
            <div className="min-w-0 stack-1">
              <h2 className="break-words" style={{ fontSize: 22, fontWeight: 700, color: "var(--ink)", letterSpacing: "-0.02em" }}>{group.name}</h2>
              <p style={{ fontSize: 13, color: "var(--text-soft)" }}>{group.destination_label}</p>
              <p style={{ fontSize: 12, fontWeight: 500, color: "var(--muted)" }}>
                {formatFreshnessLabel(progressQuery.data?.last_scraped_at ?? null)}
              </p>
            </div>
            <Badge tone={group.is_active ? "success" : "neutral"} dot>
              {group.is_active ? "Active" : "Inactive"}
            </Badge>
          </div>

          <div
            className="mt-5 grid min-w-0 grid-cols-2 gap-2 lg:grid-cols-4 xl:grid-cols-7"
            style={{ borderTop: "1px solid var(--border)", paddingTop: 18 }}
          >
            <MiniStat icon="calendar" k="Nights" v={group.nights} />
            <MiniStat icon="activity" k="Days Ahead" v={group.days_ahead} />
            <MiniStat icon="globe" k="Currency" v={group.currency} />
            <MiniStat icon="swap" k="Stops" v={formatStopModeLabel(group.max_stops)} />
            <MiniStat icon="shield" k="Airline Match" v={group.same_airline_only ? "Same airline only" : "Any airline mix"} />
            <MiniStat icon="chevright" k="Max Layover" v={group.max_layover_minutes ? `${Math.round(group.max_layover_minutes / 60)}h` : "Any"} />
            <MiniStat icon="plane" k="Max Leg Duration" v={group.max_leg_duration_minutes ? `${Math.round(group.max_leg_duration_minutes / 60)}h` : "Any"} />
          </div>

          <div
            className={`mt-5 grid min-w-0 gap-4 ${group.trip_type === "multi_city" ? "lg:grid-cols-2" : ""}`}
            style={{ borderTop: "1px solid var(--border)", paddingTop: 18 }}
          >
            <div className="min-w-0 max-w-full overflow-hidden" style={{ borderRadius: "var(--r-md)", border: "1px solid var(--border)", background: "var(--surface-soft)", padding: 14 }}>
              <p className="eyebrow" style={{ marginBottom: 8 }}>Outbound</p>
              <div className="flex max-w-full flex-wrap items-center gap-2" style={{ overflowX: "hidden" }}>
                {group.origins.map((code) => (
                  <span key={`origin-${code}`} className="badge badge--accent">{code}</span>
                ))}
                <span style={{ color: "var(--faint)" }}>-&gt;</span>
                {group.destinations.map((code) => (
                  <span key={`destination-${code}`} className="badge badge--success">{code}</span>
                ))}
              </div>
            </div>

            {group.trip_type === "multi_city" && chainLegs?.length ? (
              <div className="min-w-0 max-w-full overflow-hidden" style={{ borderRadius: "var(--r-md)", border: "1px solid var(--border)", background: "var(--surface-soft)", padding: 14 }}>
                <p className="eyebrow" style={{ marginBottom: 8 }}>
                  Onward Legs ({chainLegs.length + 1} flights total)
                </p>
                <div className="stack-2">
                  {chainLegs.map((leg, index) => (
                    <div key={`leg-${index}`} className="ds-row ds-wrap ds-gap-2" style={{ fontSize: 12 }}>
                      <span style={{ fontWeight: 500, color: "var(--muted)" }}>Leg {index + 2}:</span>
                      <span className="badge badge--warning">{leg.origin}</span>
                      <span style={{ color: "var(--faint)" }}>-&gt;</span>
                      {leg.destination ? (
                        <span className="badge badge--success">{leg.destination}</span>
                      ) : (
                        group.origins.map((code) => (
                          <span key={`home-${code}`} className="badge badge--accent">{code}</span>
                        ))
                      )}
                      <span style={{ color: "var(--muted)" }}>
                        after {leg.nights_before} night{leg.nights_before === 1 ? "" : "s"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : group.trip_type === "multi_city" && returnOrigin ? (
              <div className="min-w-0 max-w-full overflow-hidden" style={{ borderRadius: "var(--r-md)", border: "1px solid var(--border)", background: "var(--surface-soft)", padding: 14 }}>
                <p className="eyebrow" style={{ marginBottom: 8 }}>Return</p>
                <div className="flex max-w-full flex-wrap items-center gap-2" style={{ overflowX: "hidden" }}>
                  <span className="badge badge--warning">{returnOrigin}</span>
                  <span style={{ color: "var(--faint)" }}>-&gt;</span>
                  {group.origins.map((code) => (
                    <span key={`return-${code}`} className="badge badge--accent">{code}</span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </Card>

        <Card className="w-full min-w-0 max-w-full overflow-hidden">
          <h3 style={{ marginBottom: 16, fontSize: 15, fontWeight: 600, color: "var(--ink)" }}>Scrape Health</h3>
          <ScrapeHealthPanel groupId={group.id} health={progressQuery.data?.health} />
        </Card>

        <Card className="w-full min-w-0 max-w-full overflow-hidden">
          <h3 style={{ marginBottom: 16, fontSize: 15, fontWeight: 600, color: "var(--ink)" }}>Collection Progress</h3>
          {progressQuery.isLoading ? (
            <Skeleton className="h-32" />
          ) : progressQuery.isError ? (
            <p style={{ fontSize: 14, color: "var(--danger)" }}>Failed to load progress. Try refreshing the page.</p>
          ) : progressQuery.data ? (
            <DateCoverageGrid
              progress={progressQuery.data}
              windowStart={group.start_date}
              windowEnd={group.end_date}
            />
          ) : (
            <p style={{ fontSize: 14, color: "var(--muted)" }}>No data collected yet. Trigger a collection to start.</p>
          )}
        </Card>

        <Card className="w-full min-w-0 max-w-full overflow-hidden">
          <div className="ds-row ds-wrap ds-between ds-gap-4" style={{ marginBottom: 16, minWidth: 0 }}>
            <h3 style={{ fontSize: 15, fontWeight: 600, color: "var(--ink)" }}>Price Trend</h3>
            <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2 overflow-x-hidden text-sm">
              <Select
                aria-label="Select origin"
                value={activeOrigin}
                onChange={(e) => setSelectedOrigin(e.target.value)}
                className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1 text-sm font-medium text-slate-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              >
                {originOptions.map((origin) => (
                  <option key={origin} value={origin}>
                    {origin}
                  </option>
                ))}
              </Select>
              <span style={{ color: "var(--muted)" }}>-&gt;</span>
              <span className="codetag" style={{ padding: "8px 12px" }}>
                {destForQuery}
              </span>
            </div>
          </div>
          {trendQuery.isLoading ? (
            <Skeleton className="h-64" />
          ) : trendQuery.isError ? (
            <p style={{ padding: "32px 0", textAlign: "center", fontSize: 14, color: "var(--danger)" }}>Failed to load price trend data.</p>
          ) : (trendQuery.data ?? []).length === 0 ? (
            <p style={{ padding: "32px 0", textAlign: "center", fontSize: 14, color: "var(--muted)" }}>
              No price data yet for this route. Trigger a collection first.
            </p>
          ) : (
            <Suspense fallback={<Skeleton className="h-64" />}>
              <PriceChart data={trendQuery.data ?? []} currency={group.currency} />
            </Suspense>
          )}
        </Card>

        <Card pad0 className="w-full min-w-0 max-w-full overflow-hidden">
          <div className="ds-row ds-wrap ds-between ds-gap-4" style={{ minWidth: 0, padding: "20px 20px 0" }}>
            <div className="min-w-0">
              <h3 style={{ fontSize: 15, fontWeight: 600, color: "var(--ink)" }}>Price Data</h3>
              {group.trip_type === "multi_city" && returnOrigin ? (
                <p className="break-words" style={{ marginTop: 4, fontSize: 12, color: "var(--muted)" }}>
                  Each row is one full itinerary fare for {group.origins[0]} -&gt; {group.destinations[0]} and{" "}
                  {returnOrigin} -&gt; {group.origins[0]} after {group.nights} nights.
                </p>
              ) : null}
            </div>
            <div className="ds-row ds-gap-2" style={{ minWidth: 0 }}>
              <Select
                aria-label="Filter by origin"
                value={combinedAlternativeOrigin ? activeOrigin : selectedOrigin}
                onChange={(e) => setSelectedOrigin(e.target.value)}
              >
                {!combinedAlternativeOrigin ? <option value="">All origins</option> : null}
                {originOptions.map((origin) => (
                  <option key={origin} value={origin}>
                    {origin}
                  </option>
                ))}
              </Select>
              {filledPrices.length > 0 ? (
                <span style={{ fontSize: 12, color: "var(--muted)" }}>
                  {filledPrices.length} rows{priceHasMore ? "+" : ""}
                </span>
              ) : null}
            </div>
          </div>
          <PriceTable
            prices={filledPrices}
            isLoading={pricesLoading && allPrices.length === 0}
            hasMore={priceHasMore}
            onLoadMore={handlePriceLoadMore}
            loadingMore={pricesLoading && allPrices.length > 0}
            groupCurrency={group.currency}
            tripType={group.trip_type}
            nights={effectiveNights}
            returnOrigin={returnOrigin}
            homeOrigin={group.origins[0]}
            multiCityLegs={chainLegs}
          />
        </Card>

        {editOpen ? (
          <RouteGroupForm open={editOpen} onClose={() => setEditOpen(false)} initial={group} />
        ) : null}

        {downloadOpen ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(15,23,42,0.4)" }}>
            <div className="mx-4 w-full max-w-sm" style={{ borderRadius: "var(--r-lg)", background: "var(--surface)", padding: 24, boxShadow: "var(--shadow-pop)" }}>
              <h3 style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>Download Excel</h3>
              <p style={{ marginTop: 8, fontSize: 14, color: "var(--text-soft)" }}>
                Export collected fares for <span style={{ fontWeight: 500 }}>{group.name}</span>.
              </p>
              <label className="ds-row ds-gap-2 ds-start" style={{ marginTop: 16, cursor: "pointer", fontSize: 14, color: "var(--text-soft)" }}>
                <input
                  type="checkbox"
                  checked={includeLinks}
                  onChange={(e) => setIncludeLinks(e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                />
                <span>
                  Include verification links
                  <span style={{ display: "block", fontSize: 12, color: "var(--muted)" }}>
                    Adds a column with the Kayak link each fare was scraped from.
                  </span>
                </span>
              </label>
              <div className="ds-row ds-gap-2" style={{ marginTop: 20, justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setDownloadOpen(false)}>
                  Cancel
                </Button>
                <Btn variant="primary" icon="download" onClick={handleDownload} loading={downloading}>
                  Download
                </Btn>
              </div>
            </div>
          </div>
        ) : null}

        {confirmTrigger ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(15,23,42,0.4)" }}>
            <div className="mx-4 w-full max-w-sm" style={{ borderRadius: "var(--r-lg)", background: "var(--surface)", padding: 24, boxShadow: "var(--shadow-pop)" }}>
              <h3 style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>Trigger Full Scrape?</h3>
              <p style={{ marginTop: 8, fontSize: 14, color: "var(--text-soft)" }}>
                This will start a collection run for missing dates in <span style={{ fontWeight: 500 }}>{group.name}</span>.
              </p>
              <div className="ds-row ds-gap-2" style={{ marginTop: 20, justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setConfirmTrigger(false)}>
                  Cancel
                </Button>
                <Btn variant="primary" onClick={handleTrigger} loading={triggering}>
                  Yes, trigger
                </Btn>
              </div>
            </div>
          </div>
        ) : null}

        {confirmResetCaps ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(15,23,42,0.4)" }}>
            <div className="mx-4 w-full max-w-sm" style={{ borderRadius: "var(--r-lg)", background: "var(--surface)", padding: 24, boxShadow: "var(--shadow-pop)" }}>
              <h3 style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>Reset Retry Caps?</h3>
              <p style={{ marginTop: 8, fontSize: 14, color: "var(--text-soft)" }}>
                Dates that repeatedly returned no fare or errored are skipped after a
                few attempts. This clears those skipped-attempt records for{" "}
                <span style={{ fontWeight: 500, color: "var(--text)" }}>{group.name}</span> so they
                can be collected again on the next scrape.
              </p>
              <p style={{ marginTop: 8, fontSize: 14, color: "var(--text-soft)" }}>
                Your already-collected prices are <span style={{ fontWeight: 500 }}>not</span>{" "}
                deleted, and already-collected dates are not re-scraped.
              </p>
              <div className="ds-row ds-gap-2" style={{ marginTop: 20, justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setConfirmResetCaps(false)}>
                  Cancel
                </Button>
                <Btn variant="primary" icon="refresh" onClick={handleResetCaps} loading={resettingCaps}>
                  Yes, reset caps
                </Btn>
              </div>
            </div>
          </div>
        ) : null}

        {confirmDelete ? (
          <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(15,23,42,0.4)" }}>
            <div className="mx-4 w-full max-w-sm" style={{ borderRadius: "var(--r-lg)", background: "var(--surface)", padding: 24, boxShadow: "var(--shadow-pop)" }}>
              <h3 style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>Delete Route Group</h3>
              <p style={{ marginTop: 8, fontSize: 14, color: "var(--text-soft)" }}>
                Are you sure you want to delete <span style={{ fontWeight: 500, color: "var(--text)" }}>{group.name}</span>? All
                collected price data will be permanently lost.
              </p>
              <div className="ds-row ds-gap-2" style={{ marginTop: 20, justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setConfirmDelete(false)}>
                  Cancel
                </Button>
                <Btn
                  variant="danger"
                  loading={deleting}
                  onClick={async () => {
                    setDeleting(true);
                    try {
                      await deleteRouteGroup(id!);
                      await qc.invalidateQueries({ queryKey: ["route-groups"] });
                      showToast("Route group deleted", "success");
                      navigate("/", { replace: true });
                    } catch (err) {
                      showToast(getErrorMessage(err, "Failed to delete route group"), "error");
                      setDeleting(false);
                      setConfirmDelete(false);
                    }
                  }}
                >
                  {deleting ? "Deleting…" : "Delete"}
                </Btn>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </ErrorBoundary>
  );
}
