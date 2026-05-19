import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Database,
  Download,
  FolderOpen,
  Globe,
  Grid2X2,
  List,
  MapPin,
  Play,
  RefreshCw,
  Search,
  Square,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import {
  fetchCollectionRuns,
  getCollectionStatus,
  stopCollection,
  triggerCollection,
  triggerGroupCollection,
} from "../api/collection";
import { getErrorMessage } from "../api/client";
import {
  downloadExport,
  getRouteGroupProgress,
  listRouteGroups,
  saveBlobAsFile,
} from "../api/route-groups";
import { fetchHealth, fetchOverviewStats } from "../api/stats";
import { CollectionProgressBar } from "../components/CollectionProgressBar";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { ProviderStatus } from "../components/ProviderStatus";
import { RouteGroupCard } from "../components/RouteGroupCard";
import { RouteGroupForm } from "../components/RouteGroupForm";
import { StatCard } from "../components/StatCard";
import { Button } from "../components/ui/Button";
import { Skeleton } from "../components/ui/Skeleton";
import { useToast } from "../context/ToastContext";
import type { RouteGroup } from "../types/route-group";
import { formatNumber, formatRelativeTime } from "../utils/format";
import { usePageTitle } from "../utils/usePageTitle";

export function DashboardPage() {
  usePageTitle("Dashboard");

  const { showToast } = useToast();
  const qc = useQueryClient();

  const [triggering, setTriggering] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "paused">("all");
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");
  const [startProbeUntil, setStartProbeUntil] = useState<number | null>(null);

  const wasCollecting = useRef(false);

  const statsQuery = useQuery({
    queryKey: ["stats"],
    queryFn: fetchOverviewStats,
    refetchInterval: 60_000,
  });

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
    refetchInterval: (query) =>
      query.state.data?.is_collecting || (startProbeUntil != null && Date.now() < startProbeUntil)
        ? 1_500
        : 15_000,
  });

  const stopMut = useMutation({
    mutationFn: stopCollection,
    onSuccess: () => {
      showToast("Stop signal sent", "success");
      qc.invalidateQueries({ queryKey: ["collection-status"] });
    },
    onError: (error) => showToast(getErrorMessage(error, "Failed to stop collection"), "error"),
  });

  const isCollecting = statusQuery.data?.is_collecting ?? false;
  const stats = statsQuery.data;
  const groups = useMemo(() => groupsQuery.data ?? [], [groupsQuery.data]);
  const health = healthQuery.data;
  const providerStatuses = Object.values(health?.provider_status ?? {});
  const noProvider =
    !healthQuery.isLoading &&
    !providerStatuses.some((status) => status === "configured");
  const activeGroups = groups.filter((group) => group.is_active).length;
  const pausedGroups = groups.length - activeGroups;
  useEffect(() => {
    if (wasCollecting.current && !isCollecting) {
      fetchCollectionRuns(1)
        .then((runs) => {
          const last = runs[0];
          if (!last) return;

          if (last.status === "completed") {
            const errors = last.routes_failed ?? 0;
            const success = last.routes_success ?? 0;

            if (errors > 0) {
              showToast(
                `Collection finished - ${success} prices collected, ${errors} route(s) failed.`,
                "error",
              );
            } else {
              showToast(
                `Collection finished - ${success} prices collected successfully.`,
                "success",
              );
            }
          } else if (last.status === "stopped") {
            showToast("Collection was stopped.", "info");
          } else if (last.status === "failed") {
            showToast("Collection failed. Check Collection Logs for details.", "error");
          }

          qc.invalidateQueries({ queryKey: ["stats"] });
          qc.invalidateQueries({ queryKey: ["route-groups"] });
        })
        .catch(() => {});
    }

    wasCollecting.current = isCollecting;
  }, [isCollecting, qc, showToast]);

  useEffect(() => {
    if (isCollecting && startProbeUntil != null) {
      setStartProbeUntil(null);
      return;
    }

    if (startProbeUntil == null) return;

    const remaining = startProbeUntil - Date.now();
    if (remaining <= 0) {
      setStartProbeUntil(null);
      return;
    }

    const timer = window.setTimeout(() => setStartProbeUntil(null), remaining);
    return () => window.clearTimeout(timer);
  }, [isCollecting, startProbeUntil]);

  useEffect(() => {
    if (!isCollecting) return;

    qc.invalidateQueries({ queryKey: ["stats"] });
    qc.invalidateQueries({ queryKey: ["route-group-progress"] });
  }, [
    isCollecting,
    qc,
    statusQuery.data?.progress?.prices_done,
    statusQuery.data?.progress?.dates_scraped,
    statusQuery.data?.progress?.current_origin,
  ]);

  const filteredGroups = useMemo(() => {
    return groups.filter((group) => {
      const matchesSearch =
        search.trim() === "" ||
        group.name.toLowerCase().includes(search.toLowerCase()) ||
        group.destination_label.toLowerCase().includes(search.toLowerCase()) ||
        group.origins.join(" ").toLowerCase().includes(search.toLowerCase()) ||
        group.destinations.join(" ").toLowerCase().includes(search.toLowerCase());

      const matchesStatus =
        statusFilter === "all" ||
        (statusFilter === "active" ? group.is_active : !group.is_active);

      return matchesSearch && matchesStatus;
    });
  }, [groups, search, statusFilter]);

  async function handleTriggerAll() {
    setTriggering(true);

    try {
      const res = await triggerCollection();

      if (res.status === "already_running") {
        showToast("Collection is already running", "info");
      } else {
        showToast("Collection triggered successfully", "success");
        setStartProbeUntil(Date.now() + 30_000);
        qc.invalidateQueries({ queryKey: ["collection-status"] });
      }
    } catch (err) {
      showToast(getErrorMessage(err, "Failed to trigger collection"), "error");
    } finally {
      setTriggering(false);
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-8">
        <TopBar
          title="Flight Scraper Overview"
          subtitle="DASHBOARD"
          actions={
            <>
              <StatusBar
                isCollecting={isCollecting}
                schedulerRunning={health?.scheduler_running ?? false}
                databaseOk={health?.database_status === "ok"}
              />
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setCreateOpen(true)}
                className="rounded-[8px] px-3 py-1.5 text-[13px]"
              >
                New Group
              </Button>
              {isCollecting ? (
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => stopMut.mutate()}
                  loading={stopMut.isPending}
                  className="rounded-[8px] px-3 py-1.5 text-[13px]"
                >
                  <Square className="h-[13px] w-[13px]" />
                  Stop
                </Button>
              ) : (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={handleTriggerAll}
                  loading={triggering}
                  className="rounded-[8px] px-3 py-1.5 text-[13px]"
                >
                  <Play className="h-[13px] w-[13px]" />
                  Trigger
                </Button>
              )}
            </>
          }
        />

        {noProvider ? (
          <Banner
            tone="amber"
            icon={<AlertTriangle className="h-[15px] w-[15px]" />}
            title="No API key configured"
            text="Add SCRAPINGBEE_API_KEY or SCRAPINGBEE_API_KEYS."
          />
        ) : null}

        {isCollecting && statusQuery.data?.progress ? (
          <div className="rounded-[12px] border border-brand-100 bg-brand-50 p-3">
            <CollectionProgressBar progress={statusQuery.data.progress} />
          </div>
        ) : null}

        {groupsQuery.error ? (
          <Banner
            tone="amber"
            icon={<AlertTriangle className="h-[15px] w-[15px]" />}
            title="Route groups could not be loaded"
            text={getErrorMessage(groupsQuery.error, "The dashboard could not load your route groups.")}
          />
        ) : null}

        {statsQuery.error ? (
          <Banner
            tone="amber"
            icon={<AlertTriangle className="h-[15px] w-[15px]" />}
            title="Overview stats could not be loaded"
            text={getErrorMessage(statsQuery.error, "Current totals are temporarily unavailable.")}
          />
        ) : null}

        {healthQuery.error ? (
          <Banner
            tone="amber"
            icon={<AlertTriangle className="h-[15px] w-[15px]" />}
            title="Health status could not be loaded"
            text={getErrorMessage(
              healthQuery.error,
              "Provider and database checks are temporarily unavailable.",
            )}
          />
        ) : null}

        <section>
          <SectionEyebrow>OVERVIEW</SectionEyebrow>
          <div className="mt-[10px] grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {statsQuery.isLoading ? (
              [...Array(4)].map((_, index) => (
                <Skeleton key={index} className="h-[118px] rounded-[12px]" />
              ))
            ) : (
              <>
                <StatCard
                  label="Route Groups"
                  value={groups.length}
                  subtitle={`${activeGroups} active · ${pausedGroups} paused`}
                  icon={Globe}
                />
                <StatCard
                  label="Prices Collected"
                  value={stats ? formatNumber(stats.total_prices_collected) : "0"}
                  icon={Database}
                />
                <StatCard
                  label="Origins"
                  value={stats?.total_origins ?? 0}
                  icon={MapPin}
                />
                <StatCard
                  label="Last Run"
                  value={stats?.last_collection_at ? formatRelativeTime(stats.last_collection_at) : "Never"}
                  valueClassName="text-[24px]"
                  icon={Activity}
                />
              </>
            )}
          </div>
        </section>

        <section>
          <div className="mb-4 flex flex-wrap items-center gap-[10px]">
            <div className="flex-1">
              <div className="text-[15px] font-semibold text-[#1a1d23]">Route Groups</div>
              <div className="text-[12px] text-[#9CA3AF]">
                {groups.length} configured · {filteredGroups.length} shown
              </div>
            </div>

            <div className="relative">
              <Search className="pointer-events-none absolute left-[10px] top-1/2 h-[13px] w-[13px] -translate-y-1/2 text-[#9CA3AF]" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search groups..."
                className="w-[200px] rounded-[8px] border-[1.5px] border-[#E2E8F0] bg-white px-3 py-[7px] pl-8 text-[13px] text-[#1a1d23] outline-none transition focus:border-brand-600"
              />
            </div>

            <div className="flex gap-1 rounded-[8px] bg-[#F4F6FA] p-[3px]">
              {[
                { id: "all", label: "All" },
                { id: "active", label: "Active" },
                { id: "paused", label: "Paused" },
              ].map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setStatusFilter(item.id as "all" | "active" | "paused")}
                  className={`rounded-[6px] px-3 py-[5px] text-[12px] transition ${
                    statusFilter === item.id
                      ? "bg-white font-semibold text-[#1a1d23] shadow-[0_1px_3px_rgba(0,0,0,0.07)]"
                      : "font-normal text-[#9CA3AF]"
                  }`}
                >
                  {item.label}
                </button>
              ))}
            </div>

            <div className="flex gap-[2px] rounded-[8px] bg-[#F4F6FA] p-[3px]">
              <button
                type="button"
                onClick={() => setViewMode("grid")}
                className={`flex h-7 w-[30px] items-center justify-center rounded-[6px] transition ${
                  viewMode === "grid"
                    ? "bg-white text-brand-700 shadow-[0_1px_3px_rgba(0,0,0,0.07)]"
                    : "text-[#9CA3AF]"
                }`}
              >
                <Grid2X2 className="h-[13px] w-[13px]" />
              </button>
              <button
                type="button"
                onClick={() => setViewMode("list")}
                className={`flex h-7 w-[30px] items-center justify-center rounded-[6px] transition ${
                  viewMode === "list"
                    ? "bg-white text-brand-700 shadow-[0_1px_3px_rgba(0,0,0,0.07)]"
                    : "text-[#9CA3AF]"
                }`}
              >
                <List className="h-[13px] w-[13px]" />
              </button>
            </div>

            <Button
              variant="primary"
              size="sm"
              onClick={() => setCreateOpen(true)}
              className="rounded-[8px] px-3 py-1.5 text-[13px]"
            >
              Add Group
            </Button>
          </div>

          {groupsQuery.isLoading ? (
            <div className="grid gap-[14px]" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
              {[...Array(4)].map((_, index) => (
                <Skeleton key={index} className="h-64 rounded-[12px]" />
              ))}
            </div>
          ) : filteredGroups.length === 0 ? (
            <div className="py-16 text-center">
              <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-[12px] bg-[#F4F6FA]">
                <FolderOpen className="h-5 w-5 text-[#C4CAD4]" />
              </div>
              <div className="mb-1 text-[14px] font-semibold text-[#6B7280]">No groups match your search</div>
              <div className="text-[12px] text-[#9CA3AF]">Try a different keyword or filter.</div>
            </div>
          ) : viewMode === "grid" ? (
            <div className="grid gap-[14px]" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
              {filteredGroups.map((group) => (
                <RouteGroupCard key={group.id} group={group} />
              ))}
            </div>
          ) : (
            <div className="overflow-hidden rounded-[12px] border border-[#E8ECF4] bg-white">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-[#E8ECF4] bg-[#FAFBFF]">
                    {["Group", "Route", "Type", "Coverage", "Window", "Currency", "Status", ""].map((heading) => (
                      <th
                        key={heading}
                        className="whitespace-nowrap px-4 py-[10px] text-left text-[11px] font-semibold tracking-[0.05em] text-[#9CA3AF]"
                      >
                        {heading}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filteredGroups.map((group) => (
                    <DashboardGroupRow key={group.id} group={group} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="mt-8">
          <SectionEyebrow>PROVIDER STATUS</SectionEyebrow>
          <div className="mt-[10px]">
            {healthQuery.isLoading ? (
              <Skeleton className="h-[72px] rounded-[12px]" />
            ) : (
              <ProviderStatus health={health} />
            )}
          </div>
        </section>
      </div>

      {createOpen ? (
        <RouteGroupForm open={createOpen} onClose={() => setCreateOpen(false)} initial={null} />
      ) : null}
    </ErrorBoundary>
  );
}

function DashboardGroupRow({ group }: { group: RouteGroup }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { showToast } = useToast();
  const [downloading, setDownloading] = useState(false);
  const [triggering, setTriggering] = useState(false);

  const progressQuery = useQuery({
    queryKey: ["route-group-progress", group.id],
    queryFn: () => getRouteGroupProgress(group.id),
    refetchInterval: 10_000,
  });

  const progress = progressQuery.data;
  const coverage = progress ? Math.min(progress.coverage_percent, 100) : 0;
  const routeLabel = `${group.origins[0] ?? "-"}->${group.destinations[0] ?? "-"}`;
  const tripType =
    group.trip_type === "multi_city"
      ? "Multi City"
      : group.trip_type === "round_trip"
        ? "Round Trip"
        : "One Way";

  async function handleDownload(event: MouseEvent) {
    event.stopPropagation();
    setDownloading(true);

    try {
      const blob = await downloadExport(group.id);
      saveBlobAsFile(blob, `${group.name.replace(/[^a-z0-9_-]/gi, "_")}.xlsx`);
      showToast("Excel downloaded", "success");
    } catch (err) {
      showToast(getErrorMessage(err, "Download failed"), "error");
    } finally {
      setDownloading(false);
    }
  }

  async function handleTrigger(event: MouseEvent) {
    event.stopPropagation();
    setTriggering(true);

    try {
      await triggerGroupCollection(group.id);
      showToast("Collection triggered successfully", "success");
      qc.invalidateQueries({ queryKey: ["collection-status"] });
      qc.invalidateQueries({ queryKey: ["route-group-progress", group.id] });
    } catch (err) {
      showToast(getErrorMessage(err, "Failed to trigger collection"), "error");
    } finally {
      setTriggering(false);
    }
  }

  return (
    <tr
      onClick={() => navigate(`/route-groups/${group.id}`)}
      className="cursor-pointer border-b border-[#F4F6FA] bg-white transition hover:bg-[#FAFBFF]"
    >
      <td className="px-4 py-[11px]">
        <div className="text-[13px] font-semibold text-[#1a1d23]">{group.name}</div>
        <div className="text-[11px] text-[#9CA3AF]">{group.destination_label}</div>
      </td>
      <td className="px-4 py-[11px]">
        <span className="rounded-[4px] bg-[#F4F6FA] px-[7px] py-[2px] font-mono text-[12px] font-semibold text-[#6B7280]">
          {routeLabel}
        </span>
      </td>
      <td className="px-4 py-[11px]">
        <span className="rounded-full bg-[#EEF2FF] px-2 py-[2px] text-[12px] font-medium text-[#4B5EDE]">
          {tripType}
        </span>
      </td>
      <td className="min-w-[120px] px-4 py-[11px]">
        <div className="flex items-center gap-2">
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-[#EEF2FF]">
            <div
              className={`h-full rounded-full ${coverage > 90 ? "bg-brand-600" : "bg-amber-500"}`}
              style={{ width: `${coverage}%` }}
            />
          </div>
          <span className="w-[38px] text-right text-[11px] font-semibold text-[#6B7280]">
            {progress ? `${progress.coverage_percent.toFixed(0)}%` : "-"}
          </span>
        </div>
      </td>
      <td className="px-4 py-[11px] text-[13px] text-[#6B7280]">{group.days_ahead}d</td>
      <td className="px-4 py-[11px]">
        <span className="rounded-full bg-[#F1F5F9] px-2 py-[2px] text-[12px] font-medium text-[#64748B]">
          {group.currency}
        </span>
      </td>
      <td className="px-4 py-[11px]">
        <span
          className={`rounded-full px-2 py-[2px] text-[12px] font-medium ${
            group.is_active ? "bg-[#ECFDF5] text-[#059669]" : "bg-[#FFFBEB] text-[#D97706]"
          }`}
        >
          {group.is_active ? "Active" : "Paused"}
        </span>
      </td>
      <td className="px-4 py-[11px]">
        <div className="flex gap-2" onClick={(event) => event.stopPropagation()}>
          <button
            type="button"
            onClick={handleTrigger}
            disabled={triggering}
            className="flex h-[26px] w-[26px] items-center justify-center rounded-[6px] border border-[#E8ECF4] bg-white text-[#6B7280] transition hover:bg-[#F8FAFF] disabled:opacity-50"
            title="Trigger scrape"
          >
            <RefreshCw className={`h-3 w-3 ${triggering ? "animate-spin" : ""}`} />
          </button>
          <button
            type="button"
            onClick={handleDownload}
            disabled={downloading}
            className="flex h-[26px] w-[26px] items-center justify-center rounded-[6px] border border-[#E8ECF4] bg-white text-[#6B7280] transition hover:bg-[#F8FAFF] disabled:opacity-50"
            title="Download export"
          >
            <Download className="h-3 w-3" />
          </button>
        </div>
      </td>
    </tr>
  );
}

function TopBar({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
      <div>
        {subtitle ? (
          <div className="mb-[2px] text-[11px] font-medium tracking-[0.05em] text-[#9CA3AF]">
            {subtitle}
          </div>
        ) : null}
        <h1 className="text-[22px] font-bold text-[#1a1d23]">{title}</h1>
      </div>
      <div className="flex flex-wrap items-center gap-2">{actions}</div>
    </div>
  );
}

function StatusBar({
  isCollecting,
  schedulerRunning,
  databaseOk,
}: {
  isCollecting: boolean;
  schedulerRunning: boolean;
  databaseOk: boolean;
}) {
  const schedulerActive = isCollecting || schedulerRunning;

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-[6px] rounded-full border border-[#A7F3D0] bg-[#ECFDF5] px-[10px] py-[5px]">
        <span className="inline-block h-[7px] w-[7px] rounded-full bg-[#10B981] shadow-[0_0_0_2px_#D1FAE5]" />
        <span className="text-[11px] font-medium text-[#059669]">
          {schedulerActive ? "Scheduler Running" : "Scheduler Idle"}
        </span>
      </div>
      <div className="flex items-center gap-[6px] rounded-full border border-[#C7D2FE] bg-[#EEF2FF] px-[10px] py-[5px]">
        <Database className="h-[11px] w-[11px] text-brand-700" />
        <span className="text-[11px] font-medium text-brand-700">
          {databaseOk ? "DB ok" : "DB check"}
        </span>
      </div>
    </div>
  );
}

function SectionEyebrow({ children }: { children: ReactNode }) {
  return <div className="text-[11px] font-semibold tracking-[0.06em] text-[#9CA3AF]">{children}</div>;
}

function Banner({
  tone,
  icon,
  title,
  text,
}: {
  tone: "amber" | "blue";
  icon: ReactNode;
  title: string;
  text: ReactNode;
}) {
  const styles =
    tone === "amber"
      ? "border-amber-200 bg-amber-50 text-amber-800"
      : "border-blue-200 bg-blue-50 text-blue-800";

  return (
    <div className={`flex items-center gap-[10px] rounded-[10px] border px-4 py-[10px] text-[13px] ${styles}`}>
      <div className="shrink-0">{icon}</div>
      <div>
        <p className="font-semibold">{title}</p>
        <p className="opacity-90">{text}</p>
      </div>
    </div>
  );
}
