import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { triggerGroupCollection } from "../api/collection";
import { getErrorMessage } from "../api/client";
import {
  downloadExport,
  getRouteGroupProgress,
  saveBlobAsFile,
} from "../api/route-groups";
import { useToast } from "../context/ToastContext";
import type { RouteGroup } from "../types/route-group";
import { formatNumber } from "../utils/format";

import { Badge, Bar, Card, Icon, IconBtn, MiniStat } from "./ds";
import { Skeleton } from "./ui/Skeleton";

interface RouteGroupCardProps {
  group: RouteGroup;
}

export function RouteGroupCard({ group }: RouteGroupCardProps) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { showToast } = useToast();

  const [downloading, setDownloading] = useState(false);
  const [triggering, setTriggering] = useState(false);

  // Shares the "route-group-progress" cache with the dashboard's central
  // poller (same queryKey), so the card just reads that data instead of running
  // its own 10s interval. This avoids N duplicate polls per cycle.
  const progressQuery = useQuery({
    queryKey: ["route-group-progress", group.id],
    queryFn: () => getRouteGroupProgress(group.id),
    staleTime: 30_000,
  });

  const progress = progressQuery.data;
  const tripType = group.trip_type === "multi_city" ? "Multi City" : "Round Trip";
  const stayLabel = `${group.nights} nights`;
  const routeLabel = `${group.origins[0] ?? "-"} → ${group.destinations[0] ?? "-"}`;
  const coveragePct = progress ? Math.min(progress.coverage_percent, 100) : 0;
  const warn = coveragePct <= 90;

  async function handleDownload() {
    setDownloading(true);

    try {
      const blob = await downloadExport(group.id);
      const safeName = group.name.replace(/[^a-z0-9_-]/gi, "_");
      saveBlobAsFile(blob, `${safeName}.xlsx`);
      showToast("Excel downloaded", "success");
    } catch (err) {
      showToast(getErrorMessage(err, "Download failed"), "error");
    } finally {
      setDownloading(false);
    }
  }

  async function handleTrigger() {
    setTriggering(true);

    try {
      await triggerGroupCollection(group.id);
      showToast("Collection started. Progress will update shortly.", "success");
      await qc.invalidateQueries({ queryKey: ["collection-status"] });
      await qc.invalidateQueries({ queryKey: ["route-group-progress", group.id] });
    } catch (err) {
      showToast(getErrorMessage(err, "Failed to trigger collection"), "error");
    } finally {
      setTriggering(false);
    }
  }

  return (
    <Card
      hover
      className="flex h-full flex-col"
      style={{ cursor: "pointer", padding: 18 }}
      onClick={() => navigate(`/route-groups/${group.id}`)}
    >
      <div className="ds-row ds-between ds-start ds-gap-2">
        <h3 className="truncate" style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)", minWidth: 0, flex: 1 }}>
          {group.name}
        </h3>

        <div className="ds-row ds-gap-1" onClick={(event) => event.stopPropagation()}>
          <IconBtn
            icon="refresh"
            title="Trigger scrape"
            onClick={handleTrigger}
            spinning={triggering}
          />
          <IconBtn
            icon="download"
            title="Download export"
            onClick={handleDownload}
            disabled={downloading}
          />
        </div>
      </div>

      <div className="ds-row ds-wrap ds-gap-1" style={{ marginTop: 10 }}>
        <Badge tone={group.is_active ? "success" : "warning"} dot>
          {group.is_active ? "Active" : "Paused"}
        </Badge>
        <Badge tone="accent">{tripType}</Badge>
        <Badge tone="neutral">{group.currency}</Badge>
      </div>

      <div className="ds-row ds-gap-2" style={{ marginTop: 14, fontSize: 12, color: "var(--muted)" }}>
        <Icon name="pin" size={13} />
        <span className="truncate" style={{ minWidth: 0, flex: 1 }}>
          {group.destination_label}
        </span>
        <span className="codetag" style={{ marginLeft: "auto" }}>{routeLabel}</span>
      </div>

      <div className="ds-grid ds-g-3" style={{ marginTop: 14, gap: 8 }}>
        <MiniStat icon="globe" k="Origins" v={group.origins.length} />
        <MiniStat icon="calendar" k="Stay" v={stayLabel} />
        <MiniStat icon="activity" k="Window" v={`${group.days_ahead}d`} />
      </div>

      <div style={{ marginTop: "auto", paddingTop: 14, borderTop: "1px solid var(--border)" }}>
        {progressQuery.isLoading ? (
          <div className="stack-2">
            <Skeleton className="h-2 w-full rounded-full" />
            <Skeleton className="h-4 w-36 rounded-md" />
          </div>
        ) : progress ? (
          <>
            <div className="ds-row ds-between" style={{ marginBottom: 6 }}>
              <span style={{ fontSize: 11, color: "var(--muted)" }}>
                {formatNumber(progress.dates_with_data)} / {formatNumber(progress.total_dates)} scanned
              </span>
              <span style={{ fontSize: 11, fontWeight: 600, color: warn ? "var(--warning)" : "var(--success)" }}>
                {progress.coverage_percent.toFixed(1)}%
              </span>
            </div>
            <Bar pct={coveragePct} warn={warn} />
          </>
        ) : (
          <>
            <div className="ds-row ds-between" style={{ marginBottom: 6 }}>
              <span style={{ fontSize: 11, color: "var(--muted)" }}>No collection yet</span>
              <span style={{ fontSize: 11, fontWeight: 600, color: "var(--muted)" }}>0%</span>
            </div>
            <Bar pct={0} />
          </>
        )}
      </div>
    </Card>
  );
}
