import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, MapPin, RefreshCw } from "lucide-react";
import { useState, type ReactNode } from "react";
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

import { Card } from "./ui/Card";
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

  const progressQuery = useQuery({
    queryKey: ["route-group-progress", group.id],
    queryFn: () => getRouteGroupProgress(group.id),
    refetchInterval: 10_000,
  });

  const progress = progressQuery.data;
  const tripType =
    group.trip_type === "round_trip"
      ? "Round Trip"
      : group.trip_type === "multi_city"
        ? "Multi City"
        : "One Way";
  const stayLabel =
    group.trip_type === "multi_city"
      ? `${group.nights} nights`
      : group.trip_type === "round_trip"
        ? `${group.nights} nights`
        : "-";
  const routeLabel = `${group.origins[0] ?? "-"}->${group.destinations[0] ?? "-"}`;
  const coveragePct = progress ? Math.min(progress.coverage_percent, 100) : 0;

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
      className="cursor-pointer rounded-[12px] border-[#E8ECF4] bg-white p-[18px] shadow-none transition-[box-shadow] duration-150 hover:shadow-[0_4px_18px_rgba(75,94,222,0.08)]"
      onClick={() => navigate(`/route-groups/${group.id}`)}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="mb-1 truncate text-[14px] font-semibold text-[#1a1d23]">
            {group.name}
          </h3>
          <div className="flex flex-wrap gap-[5px]">
            <StatusBadge active={group.is_active} />
            <Badge tone="blue">{tripType}</Badge>
            <Badge tone="slate">{group.currency}</Badge>
          </div>
        </div>

        <div className="flex shrink-0 gap-1" onClick={(event) => event.stopPropagation()}>
          <IconButton
            title="Trigger scrape"
            onClick={handleTrigger}
            spinning={triggering}
            icon={<RefreshCw className="h-4 w-4" />}
          />
          <IconButton
            title="Download export"
            onClick={handleDownload}
            disabled={downloading}
            icon={<Download className="h-4 w-4" />}
          />
        </div>
      </div>

      <div className="mt-[14px] flex items-center gap-[6px]">
        <MapPin className="h-3 w-3 shrink-0 text-[#9CA3AF]" />
        <span className="truncate text-[12px] text-[#9CA3AF]">{group.destination_label}</span>
        <span className="ml-1 text-[12px] text-[#C4CAD4]">·</span>
        <span className="rounded-[4px] bg-[#F4F6FA] px-[6px] py-[1px] font-mono text-[11px] font-semibold text-[#6B7280]">
          {routeLabel}
        </span>
      </div>

      <div className="mt-[14px] grid grid-cols-3 gap-2">
        <MiniStat label="Origins" value={String(group.origins.length)} />
        <MiniStat label="Stay" value={stayLabel} />
        <MiniStat label="Window" value={`${group.days_ahead}d`} />
      </div>

      {group.last_auto_pause_note ? (
        <div className="mt-3 rounded-[10px] bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
          {group.last_auto_pause_note}
        </div>
      ) : group.consecutive_operational_failures > 0 ? (
        <div className="mt-3 rounded-[10px] bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
          Operational failure streak: {group.consecutive_operational_failures}
        </div>
      ) : null}

      <div className="mt-[14px]">
        {progressQuery.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-2 w-full rounded-full" />
            <Skeleton className="h-4 w-36 rounded-md" />
          </div>
        ) : progress ? (
          <div className="space-y-2">
            <div className="mb-[5px] flex items-center justify-between">
              <span className="text-[11px] text-[#9CA3AF]">
                {formatNumber(progress.dates_with_data)} / {formatNumber(progress.total_dates)} scanned
              </span>
              <span className={`text-[11px] font-semibold ${coveragePct > 90 ? "text-[#059669]" : "text-[#D97706]"}`}>
                {progress.coverage_percent.toFixed(1)}%
              </span>
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-[#EEF2FF]">
              <div
                className="h-full rounded-full transition-[width] duration-300"
                style={{
                  width: `${coveragePct}%`,
                  background:
                    coveragePct > 90 ? "linear-gradient(90deg,#4B5EDE,#7C3AED)" : "#F59E0B",
                }}
              />
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="mb-[5px] flex items-center justify-between">
              <span className="text-[11px] text-[#9CA3AF]">No collection yet</span>
              <span className="text-[11px] font-semibold text-[#9CA3AF]">0%</span>
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-[#EEF2FF]">
              <div className="h-full w-0 rounded-full bg-brand-600" />
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

function Badge({
  children,
  tone,
}: {
  children: ReactNode;
  tone: "blue" | "slate";
}) {
  const styles =
    tone === "blue"
      ? "bg-[#EEF2FF] text-[#4B5EDE]"
      : "bg-[#F1F5F9] text-[#64748B]";

  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-[2px] text-[12px] font-medium ${styles}`}>
      {children}
    </span>
  );
}

function StatusBadge({ active }: { active: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
        active ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"
      }`}
    >
      <span className={`h-[5px] w-[5px] rounded-full ${active ? "bg-emerald-500" : "bg-amber-500"}`} />
      {active ? "Active" : "Paused"}
    </span>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[7px] bg-[#F8FAFF] px-[10px] py-[7px]">
      <p className="mb-[1px] text-[10px] font-medium text-[#9CA3AF]">{label}</p>
      <p className="text-[13px] font-semibold text-[#1a1d23]">{value}</p>
    </div>
  );
}

function IconButton({
  title,
  onClick,
  icon,
  disabled,
  spinning = false,
}: {
  title: string;
  onClick: () => void;
  icon: ReactNode;
  disabled?: boolean;
  spinning?: boolean;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      disabled={disabled}
      className="flex h-7 w-7 items-center justify-center rounded-[7px] border border-[#E8ECF4] bg-white text-[#6B7280] transition hover:bg-[#F8FAFF] disabled:opacity-50"
    >
      <span className={spinning ? "animate-spin" : ""}>{icon}</span>
    </button>
  );
}
