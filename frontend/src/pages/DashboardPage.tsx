import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Database, Globe, MapPin } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
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
import {
  Banner,
  Bar,
  Btn,
  Card,
  Empty,
  IconBtn,
  PageHeader,
  SearchInput,
  SectionHead,
  Seg,
  StatusChip,
} from "../components/ds";
import { Skeleton } from "../components/ui/Skeleton";
import { useToast } from "../context/ToastContext";
import type { RouteGroup } from "../types/route-group";
import { formatDisplayDateTime, formatNumber } from "../utils/format";
import { usePageTitle } from "../utils/usePageTitle";

export function DashboardPage() {
  usePageTitle("Dashboard");

  const { showToast } = useToast();
  const qc = useQueryClient();

  const [triggering, setTriggering] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "needs_collection" | "collected" | "paused">("all");
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
  // "No API key configured" must reflect a genuinely UNconfigured provider,
  // not one that is merely in a temporary cooldown. The backend reports
  // "disabled" only when no key is present; a configured provider that is
  // cooling down reports "cooldown"/"rate_limited"/"quota_exhausted"/etc. and
  // must NOT trigger this banner (that briefly showed the wrong message while
  // collection was actually working). Only flag when there are provider entries
  // and every one of them is "disabled".
  const noProvider =
    !healthQuery.isLoading &&
    providerStatuses.length > 0 &&
    providerStatuses.every((status) => status === "disabled");
  const activeGroups = groups.filter((group) => group.is_active).length;
  const pausedGroups = groups.length - activeGroups;
  const lastRunDisplay = useMemo(() => {
    if (!stats?.last_collection_at) {
      return { date: "Never", time: "" };
    }
    const [runDate, ...timeParts] = formatDisplayDateTime(stats.last_collection_at).split(" ");
    return {
      date: runDate || "Never",
      time: timeParts.join(" "),
    };
  }, [stats?.last_collection_at]);
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
          } else if (last.status === "partial") {
            showToast("Collection finished with missing fare dates. Scheduler will retry the gaps automatically.", "error");
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
    statusQuery.data?.progress?.checks_done,
    statusQuery.data?.progress?.dates_scraped,
    statusQuery.data?.progress?.current_origin,
  ]);

  // Poll progress fast only while a collection is actually running; idle dashboards
  // poll slowly so we don't hammer the API with N requests every 10s for nothing.
  const progressRefetchInterval = isCollecting ? 10_000 : 60_000;
  const progressQueries = useQueries({
    queries: groups.map((group) => ({
      queryKey: ["route-group-progress", group.id],
      queryFn: () => getRouteGroupProgress(group.id),
      refetchInterval: progressRefetchInterval,
      staleTime: 8_000,
    })),
  });

  const progressByGroupId = useMemo(
    () => Object.fromEntries(groups.map((group, index) => [group.id, progressQueries[index]?.data])),
    [groups, progressQueries],
  );

  const matchedGroups = useMemo(() => {
    return groups.filter((group) => (
      search.trim() === "" ||
      group.name.toLowerCase().includes(search.toLowerCase()) ||
      group.destination_label.toLowerCase().includes(search.toLowerCase()) ||
      group.origins.join(" ").toLowerCase().includes(search.toLowerCase()) ||
      group.destinations.join(" ").toLowerCase().includes(search.toLowerCase())
    ));
  }, [groups, search]);

  const groupedGroups = useMemo(() => {
    const needsCollection: RouteGroup[] = [];
    const collected: RouteGroup[] = [];
    const paused: RouteGroup[] = [];

    for (const group of matchedGroups) {
      const progress = progressByGroupId[group.id];
      // A group that's substantially scanned (>= 95%) counts as Collected even
      // if it's paused -- the data is there, so don't bury it under Paused.
      const isCollected = progress != null && progress.total_dates > 0 && progress.coverage_percent >= 95;
      if (isCollected) {
        collected.push(group);
      } else if (!group.is_active) {
        paused.push(group);
      } else {
        needsCollection.push(group);
      }
    }

    if (statusFilter === "needs_collection") {
      return { needsCollection, collected: [], paused: [] };
    }
    if (statusFilter === "collected") {
      return { needsCollection: [], collected, paused: [] };
    }
    if (statusFilter === "paused") {
      return { needsCollection: [], collected: [], paused };
    }
    return { needsCollection, collected, paused };
  }, [matchedGroups, progressByGroupId, statusFilter]);

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
      <div className="stack-6">
        <PageHeader eyebrow="Dashboard" title="Flight Scraper Overview">
          <StatusChip tone="ok" dot>
            {isCollecting || (health?.scheduler_running ?? false) ? "Scheduler Running" : "Scheduler Idle"}
          </StatusChip>
          <StatusChip tone="info" icon="database">
            {health?.database_status === "ok" ? "DB ok" : "DB check"}
          </StatusChip>
          <Btn variant="secondary" size="sm" icon="plus" onClick={() => setCreateOpen(true)}>
            New group
          </Btn>
          {isCollecting ? (
            <Btn variant="danger" size="sm" icon="square" onClick={() => stopMut.mutate()} loading={stopMut.isPending}>
              Stop
            </Btn>
          ) : (
            <Btn variant="primary" size="sm" icon="play" onClick={handleTriggerAll} loading={triggering}>
              Trigger
            </Btn>
          )}
        </PageHeader>

        {noProvider ? (
          <Banner tone="warn" icon="alert" title="No API key configured">
            Add SCRAPINGBEE_API_KEY or SCRAPINGBEE_API_KEYS.
          </Banner>
        ) : null}

        {isCollecting && statusQuery.data?.progress ? (
          <Card style={{ background: "var(--accent-50)", borderColor: "var(--accent-100)" }}>
            <CollectionProgressBar progress={statusQuery.data.progress} />
          </Card>
        ) : null}

        {groupsQuery.error ? (
          <Banner tone="warn" icon="alert" title="Route groups could not be loaded">
            {getErrorMessage(groupsQuery.error, "The dashboard could not load your route groups.")}
          </Banner>
        ) : null}

        {statsQuery.error ? (
          <Banner tone="warn" icon="alert" title="Overview stats could not be loaded">
            {getErrorMessage(statsQuery.error, "Current totals are temporarily unavailable.")}
          </Banner>
        ) : null}

        {healthQuery.error ? (
          <Banner tone="warn" icon="alert" title="Health status could not be loaded">
            {getErrorMessage(
              healthQuery.error,
              "Provider and database checks are temporarily unavailable.",
            )}
          </Banner>
        ) : null}

        <section>
          <div className="eyebrow" style={{ marginBottom: 10 }}>Overview</div>
          <div className="ds-grid ds-g-4">
            {statsQuery.isLoading ? (
              [...Array(4)].map((_, index) => (
                <Skeleton key={index} className="h-[118px] rounded-[12px]" />
              ))
            ) : (
              <>
                <StatCard
                  label="Route Groups"
                  value={groups.length}
                  subtitle={`${activeGroups} active | ${pausedGroups} paused`}
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
                  value={lastRunDisplay.date}
                  subtitle={lastRunDisplay.time ? `Completed at ${lastRunDisplay.time}` : undefined}
                  valueClassName="text-[24px] tracking-[-0.02em]"
                  icon={Activity}
                />
              </>
            )}
          </div>
        </section>

        <section>
          <SectionHead title="Route Groups" sub={`${groups.length} configured · ${matchedGroups.length} shown`}>
            <SearchInput value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search groups…" width={200} />
            <Seg
              value={statusFilter}
              onChange={(id) => setStatusFilter(id)}
              options={[
                { id: "all", label: "All" },
                { id: "needs_collection", label: "Needs Collection" },
                { id: "collected", label: "Collected" },
                { id: "paused", label: "Paused" },
              ]}
            />
            <Seg
              value={viewMode}
              onChange={setViewMode}
              icons
              options={[
                { id: "grid", icon: "grid" },
                { id: "list", icon: "list" },
              ]}
            />
            <Btn variant="primary" size="sm" icon="plus" onClick={() => setCreateOpen(true)}>
              Add group
            </Btn>
          </SectionHead>

          {groupsQuery.isLoading ? (
            <div className="cards-auto">
              {[...Array(4)].map((_, index) => (
                <Skeleton key={index} className="h-64 rounded-[12px]" />
              ))}
            </div>
          ) : matchedGroups.length === 0 ? (
            <Empty icon="search" title="No groups match your search" text="Try a different keyword or filter." />
          ) : viewMode === "grid" ? (
            <div className="stack-5">
              <RouteGroupSection title="Needs Collection" groups={groupedGroups.needsCollection} />
              <RouteGroupSection title="Paused" groups={groupedGroups.paused} />
              <RouteGroupSection title="Collected" groups={groupedGroups.collected} />
            </div>
          ) : (
            <div className="stack-5">
              <RouteGroupTableSection title="Needs Collection" groups={groupedGroups.needsCollection} />
              <RouteGroupTableSection title="Paused" groups={groupedGroups.paused} />
              <RouteGroupTableSection title="Collected" groups={groupedGroups.collected} />
            </div>
          )}
        </section>

        <section>
          <div className="eyebrow" style={{ marginBottom: 10 }}>Provider Status</div>
          {healthQuery.isLoading ? (
            <Skeleton className="h-[72px] rounded-[12px]" />
          ) : (
            <ProviderStatus health={health} />
          )}
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
      : "Round Trip";

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
      className="clickable"
    >
      <td>
        <div style={{ fontWeight: 600, color: "var(--ink)" }}>{group.name}</div>
        <div style={{ fontSize: 11, color: "var(--muted)" }}>{group.destination_label}</div>
      </td>
      <td><span className="codetag">{routeLabel}</span></td>
      <td><span className="badge badge--accent">{tripType}</span></td>
      <td style={{ minWidth: 130 }}>
        <div className="ds-row ds-gap-2">
          <div className="ds-grow">
            <Bar pct={coverage} warn={coverage <= 90} />
          </div>
          <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-soft)", width: 36, textAlign: "right" }}>
            {progress ? `${progress.coverage_percent.toFixed(0)}%` : "-"}
          </span>
        </div>
      </td>
      <td style={{ color: "var(--text-soft)" }}>{group.days_ahead}d</td>
      <td><span className="badge badge--neutral">{group.currency}</span></td>
      <td>
        <span className={`badge badge--${group.is_active ? "success" : "warning"}`}>
          {group.is_active ? "Active" : "Paused"}
        </span>
      </td>
      <td onClick={(event) => event.stopPropagation()}>
        <div className="ds-row ds-gap-1">
          <IconBtn icon="refresh" title="Trigger scrape" onClick={handleTrigger} spinning={triggering} />
          <IconBtn icon="download" title="Download export" onClick={handleDownload} disabled={downloading} />
        </div>
      </td>
    </tr>
  );
}

function RouteGroupSection({
  title,
  groups,
}: {
  title: string;
  groups: RouteGroup[];
}) {
  if (groups.length === 0) {
    return null;
  }

  return (
    <div className="stack-3">
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-soft)" }}>
        {title} <span style={{ color: "var(--muted)", fontWeight: 400 }}>· {groups.length}</span>
      </div>
      <div className="cards-auto">
        {groups.map((group) => (
          <RouteGroupCard key={group.id} group={group} />
        ))}
      </div>
    </div>
  );
}

function RouteGroupTableSection({
  title,
  groups,
}: {
  title: string;
  groups: RouteGroup[];
}) {
  if (groups.length === 0) {
    return null;
  }

  return (
    <div className="stack-3">
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-soft)" }}>
        {title} <span style={{ color: "var(--muted)", fontWeight: 400 }}>· {groups.length}</span>
      </div>
      <Card pad0 style={{ overflow: "hidden" }}>
        <table className="tbl">
          <thead>
            <tr>
              {["Group", "Route", "Type", "Coverage", "Window", "Currency", "Status", ""].map((heading) => (
                <th key={heading}>{heading}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <DashboardGroupRow key={group.id} group={group} />
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
