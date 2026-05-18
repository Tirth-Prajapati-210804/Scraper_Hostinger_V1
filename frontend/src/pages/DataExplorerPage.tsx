import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  Database,
  Download,
  Play,
  Search,
} from "lucide-react";
import { type ReactNode, useCallback, useRef, useState } from "react";

import { getCollectionStatus, triggerCollection } from "../api/collection";
import { getErrorMessage } from "../api/client";
import { fetchPriceTrend, fetchPrices } from "../api/prices";
import { listRouteGroups } from "../api/route-groups";
import { fetchHealth } from "../api/stats";
import { DateRangeInput } from "../components/ui/DateRangeInput";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Select } from "../components/ui/Select";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { PriceChart } from "../components/PriceChart";
import { PriceTable } from "../components/PriceTable";
import { useToast } from "../context/ToastContext";
import type { DailyPrice } from "../types/price";
import { usePageTitle } from "../utils/usePageTitle";

interface Filters {
  route_group_id: string;
  origin: string;
  date_from: string;
  date_to: string;
}

const EMPTY_FILTERS: Filters = {
  route_group_id: "",
  origin: "",
  date_from: "",
  date_to: "",
};

const PAGE_SIZE = 100;

function exportCsv(rows: DailyPrice[]) {
  const header = "Date,Origin,Destination,Airline,Price,Currency,Stops,Duration(min),Provider\n";
  const lines = rows.map((row) =>
    [
      row.depart_date,
      row.origin,
      row.destination,
      row.airline,
      row.price,
      row.currency ?? "",
      row.stops ?? "",
      row.duration_minutes ?? "",
      row.provider,
    ].join(","),
  );

  const blob = new Blob([header + lines.join("\n")], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "prices.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

export function DataExplorerPage() {
  usePageTitle("Data Explorer");

  const { showToast } = useToast();
  const [pending, setPending] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [allPrices, setAllPrices] = useState<DailyPrice[]>([]);
  const [pricesLoading, setPricesLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const offsetRef = useRef(0);

  const groupsQuery = useQuery({
    queryKey: ["route-groups"],
    queryFn: listRouteGroups,
  });

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  });

  const statusQuery = useQuery({
    queryKey: ["collection-status"],
    queryFn: getCollectionStatus,
    refetchInterval: 15_000,
  });

  const selectedGroup = groupsQuery.data?.find((group) => group.id === pending.route_group_id);

  const loadPrices = useCallback(
    async (filters: Filters, newOffset: number) => {
      if (!filters.route_group_id) return;

      setPricesLoading(true);
      try {
        const data = await fetchPrices({
          route_group_id: filters.route_group_id,
          origin: filters.origin || undefined,
          date_from: filters.date_from || undefined,
          date_to: filters.date_to || undefined,
          limit: PAGE_SIZE,
          offset: newOffset,
        });

        setAllPrices((prev) => (newOffset === 0 ? data : [...prev, ...data]));
        setHasMore(data.length === PAGE_SIZE);
        offsetRef.current = newOffset;
      } catch (err) {
        setHasMore(false);
        showToast(getErrorMessage(err, "Failed to load prices"), "error");
      } finally {
        setPricesLoading(false);
      }
    },
    [showToast],
  );

  function handleApply() {
    if (!pending.route_group_id) return;
    const next = { ...pending };
    setApplied(next);
    setAllPrices([]);
    void loadPrices(next, 0);
  }

  function handleReset() {
    setPending(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setAllPrices([]);
    setHasMore(false);
    offsetRef.current = 0;
  }

  const handleLoadMore = useCallback(() => {
    void loadPrices(applied, offsetRef.current + PAGE_SIZE);
  }, [applied, loadPrices]);

  async function handleTrigger() {
    setTriggering(true);
    try {
      const result = await triggerCollection();
      if (result.status === "already_running") {
        showToast("Collection is already running", "info");
      } else {
        showToast("Collection triggered successfully", "success");
      }
    } catch (err) {
      showToast(getErrorMessage(err, "Failed to trigger collection"), "error");
    } finally {
      setTriggering(false);
    }
  }

  const appliedGroup = groupsQuery.data?.find((group) => group.id === applied.route_group_id);
  const trendOrigin = applied.origin || appliedGroup?.origins[0] || "";
  const trendDest = appliedGroup?.destinations[0] || "";

  const trendQuery = useQuery({
    queryKey: ["explorer-trend", applied, trendOrigin, trendDest],
    queryFn: () =>
      fetchPriceTrend({
        origin: trendOrigin,
        destination: trendDest,
        date_from: applied.date_from || undefined,
        date_to: applied.date_to || undefined,
      }),
    enabled: !!trendOrigin && !!trendDest,
  });

  const providerStatuses = Object.values(healthQuery.data?.provider_status ?? {});
  const noProvider =
    !healthQuery.isLoading &&
    !providerStatuses.some((status) => status === "configured" || status === "active") &&
    !healthQuery.data?.demo_mode;

  return (
    <ErrorBoundary>
      <div className="space-y-8">
        <section className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <button
              type="button"
              className="inline-flex h-11 w-11 items-center justify-center rounded-xl border border-[#e6ebf2] bg-white text-[#16213d] shadow-[0_10px_30px_-24px_rgba(15,23,42,0.45)]"
              aria-label="Explorer menu"
            >
              <BarChart3 className="h-5 w-5" />
            </button>
            <div>
              <h1 className="text-[42px] font-bold tracking-[-0.04em] text-[#111c3d]">
                Data Explorer
              </h1>
              <p className="mt-2 text-[16px] text-[#7082a3]">
                Explore and analyze collected flight price data before exporting.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <StatusPill
              tone="green"
              icon={<span className="h-2.5 w-2.5 rounded-full bg-emerald-500" />}
              text={
                statusQuery.data?.is_collecting || healthQuery.data?.scheduler_running
                  ? "Scheduler Running"
                  : "Scheduler Idle"
              }
            />
            <StatusPill
              tone="green"
              icon={<Database className="h-4 w-4" />}
              text={healthQuery.data?.database_status === "ok" ? "DB OK" : "DB Check"}
            />
            <Button
              variant="primary"
              onClick={handleTrigger}
              loading={triggering}
              className="h-12 rounded-2xl px-6 text-[17px] font-semibold shadow-[0_18px_44px_-30px_rgba(37,99,235,0.8)]"
            >
              <Play className="h-4 w-4" />
              Trigger
            </Button>
          </div>
        </section>

        {noProvider ? (
          <div className="rounded-[18px] border border-amber-200 bg-amber-50 px-5 py-4 text-sm text-amber-800">
            Add a valid ScrapingBee API key in the backend environment before running new collections.
          </div>
        ) : null}

        <Card className="rounded-[28px] border border-[#e5ebf3] bg-white p-6 shadow-[0_28px_70px_-52px_rgba(15,23,42,0.35)]">
          <div className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr_1.8fr_auto] xl:items-end">
            <Select
              label="Route Group"
              value={pending.route_group_id}
              onChange={(e) =>
                setPending({
                  route_group_id: e.target.value,
                  origin: "",
                  date_from: "",
                  date_to: "",
                })
              }
            >
              <option value="">Select route group</option>
              {groupsQuery.data?.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </Select>

            <Select
              label="Origin"
              value={pending.origin}
              onChange={(e) => setPending((current) => ({ ...current, origin: e.target.value }))}
              disabled={!selectedGroup}
            >
              <option value="">All origins</option>
              {selectedGroup?.origins.map((origin) => (
                <option key={origin} value={origin}>
                  {origin}
                </option>
              ))}
            </Select>

            <div>
              <p className="mb-2 text-[13px] font-medium text-[#4c5d7c]">Travel Window</p>
              <DateRangeInput
                dateFrom={pending.date_from}
                dateTo={pending.date_to}
                onDateFromChange={(value) => setPending((current) => ({ ...current, date_from: value }))}
                onDateToChange={(value) => setPending((current) => ({ ...current, date_to: value }))}
              />
            </div>

            <div className="flex gap-3 xl:justify-end">
              <Button
                variant="primary"
                onClick={handleApply}
                disabled={!pending.route_group_id}
                className="h-12 rounded-2xl px-6 text-[16px] font-semibold"
              >
                Apply
              </Button>
              <Button
                variant="secondary"
                onClick={handleReset}
                className="h-12 rounded-2xl px-6 text-[16px]"
              >
                Reset
              </Button>
            </div>
          </div>
        </Card>

        {!applied.route_group_id ? (
          <>
            <Card className="rounded-[28px] border border-[#e5ebf3] bg-white px-10 py-24 text-center shadow-[0_28px_70px_-52px_rgba(15,23,42,0.35)]">
              <div className="mx-auto flex h-[108px] w-[108px] items-center justify-center rounded-[28px] bg-[#f5f7ff] text-[#c9d4eb]">
                <Search className="h-14 w-14" />
              </div>
              <h2 className="mt-8 text-[32px] font-bold tracking-[-0.03em] text-[#121c39]">
                Select filters to explore data
              </h2>
              <p className="mx-auto mt-4 max-w-[520px] text-[17px] leading-8 text-[#7183a6]">
                Choose a route group and date range, then click Apply to load price trends and results.
              </p>
            </Card>

            <Card className="rounded-[24px] border border-[#e5ebf3] bg-white p-0 shadow-[0_28px_70px_-52px_rgba(15,23,42,0.35)]">
              <div className="grid gap-0 md:grid-cols-3">
                <FeatureBlurb
                  icon={<Search className="h-6 w-6" />}
                  title="Narrow your search"
                  text="Use filters above to focus on specific routes, origins, and travel dates."
                />
                <FeatureBlurb
                  icon={<BarChart3 className="h-6 w-6" />}
                  title="Explore trends"
                  text="View price trends over time and compare airlines, stops, and durations."
                  bordered
                />
                <FeatureBlurb
                  icon={<Download className="h-6 w-6" />}
                  title="Export your data"
                  text="Export the results to Excel or CSV for deeper analysis and reporting."
                  bordered
                />
              </div>
            </Card>
          </>
        ) : (
          <>
            <Card className="rounded-[28px] border border-[#e5ebf3] bg-white p-6 shadow-[0_28px_70px_-52px_rgba(15,23,42,0.35)]">
              <div className="mb-6 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <p className="text-[12px] font-semibold uppercase tracking-[0.18em] text-[#94a3b8]">
                    Trend
                  </p>
                  <h2 className="mt-1 text-[18px] font-semibold text-[#121c39]">Price Trend</h2>
                </div>
                {trendOrigin && trendDest ? (
                  <div className="inline-flex items-center gap-3 rounded-full border border-[#e2e8f0] bg-[#f8faff] px-4 py-2 text-sm text-[#62738f]">
                    <span>{trendOrigin}</span>
                    <span className="text-[#b7c2d5]">→</span>
                    <span>{trendDest}</span>
                  </div>
                ) : null}
              </div>

              {trendQuery.isError ? (
                <p className="py-8 text-center text-sm text-red-500">Failed to load price trend data.</p>
              ) : (
                <PriceChart data={trendQuery.data ?? []} />
              )}
            </Card>

            <Card className="overflow-hidden rounded-[28px] border border-[#e5ebf3] bg-white p-0 shadow-[0_28px_70px_-52px_rgba(15,23,42,0.35)]">
              <div className="border-b border-[#edf1f7] px-6 py-5">
                <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                  <div>
                    <p className="text-[12px] font-semibold uppercase tracking-[0.18em] text-[#94a3b8]">
                      Results
                    </p>
                    <h2 className="mt-1 text-[18px] font-semibold text-[#121c39]">
                      Collected Prices
                    </h2>
                  </div>
                  {allPrices.length > 0 ? (
                    <Button
                      variant="secondary"
                      onClick={() => exportCsv(allPrices)}
                      className="h-11 rounded-2xl px-5"
                    >
                      <Download className="h-4 w-4" />
                      Export CSV
                    </Button>
                  ) : null}
                </div>
              </div>

              <PriceTable
                prices={allPrices}
                isLoading={pricesLoading && allPrices.length === 0}
                hasMore={hasMore}
                onLoadMore={handleLoadMore}
                loadingMore={pricesLoading && allPrices.length > 0}
                groupCurrency={appliedGroup?.currency}
              />
            </Card>
          </>
        )}
      </div>
    </ErrorBoundary>
  );
}

function StatusPill({
  tone,
  icon,
  text,
}: {
  tone: "green" | "blue";
  icon: ReactNode;
  text: string;
}) {
  const styles =
    tone === "green"
      ? "border-[#d8f1e7] bg-white text-[#0f172a]"
      : "border-[#dbe5ff] bg-white text-[#0f172a]";

  return (
    <div
      className={`inline-flex h-12 items-center gap-3 rounded-2xl border px-5 text-[15px] font-medium shadow-[0_14px_40px_-34px_rgba(15,23,42,0.35)] ${styles}`}
    >
      <span className={tone === "green" ? "text-emerald-500" : "text-brand-600"}>{icon}</span>
      <span>{text}</span>
    </div>
  );
}

function FeatureBlurb({
  icon,
  title,
  text,
  bordered = false,
}: {
  icon: ReactNode;
  title: string;
  text: string;
  bordered?: boolean;
}) {
  return (
    <div className={`flex items-start gap-5 px-7 py-8 ${bordered ? "md:border-l md:border-[#edf1f7]" : ""}`}>
      <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-[20px] bg-[#f2f6ff] text-brand-600">
        {icon}
      </div>
      <div>
        <h3 className="text-[18px] font-semibold text-[#101935]">{title}</h3>
        <p className="mt-2 max-w-[290px] text-[15px] leading-7 text-[#7183a6]">{text}</p>
      </div>
    </div>
  );
}
