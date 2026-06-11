import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, CheckCircle2, Clock } from "lucide-react";
import { useMemo } from "react";

import { fetchScrapeLogs } from "../api/collection";
import type { ScrapeLogEntry } from "../types/price";
import type { ScrapeHealth } from "../types/route-group";
import { formatFreshnessLabel } from "../utils/format";
import { Skeleton } from "./ui/Skeleton";

interface ScrapeHealthPanelProps {
  groupId: string;
  health: ScrapeHealth | undefined;
}

const REASON_LABELS: Record<string, string> = {
  success: "Saved a fare",
  filtered_out: "No valid fare",
  page_empty: "No flights",
  extract_failed: "Extract failed",
  market_mismatch: "Wrong market",
};

const STATUS_LABELS: Record<string, string> = {
  provider_error: "Provider error",
  parse_error: "Parse error",
  rate_limited: "Rate limited",
  quota_exhausted: "Quota exhausted",
  auth_error: "Auth error",
  stopped: "Stopped",
};

// New errors are already stored short (backend _friendly_error), but LEGACY rows may
// still hold a long raw ScrapingBee 500 body. Collapse those to a clean line and cap
// length so the panel never shows the verbose "try premium_proxy ... 75 credits" blob.
function shortenError(message: string): string {
  const lowered = message.toLowerCase();
  if (lowered.includes("error with your request") || lowered.includes("you will not be charged")) {
    return "Kayak page did not render in time - will retry.";
  }
  if (lowered.includes("timed out") || lowered.includes("timeout")) {
    return "Kayak render timed out - will retry.";
  }
  const trimmed = message.trim();
  return trimmed.length > 140 ? `${trimmed.slice(0, 140)}…` : trimmed;
}

function outcomeLabel(log: ScrapeLogEntry): string {
  if (log.status === "success") return REASON_LABELS.success;
  if (log.status === "no_results") {
    return REASON_LABELS[log.result_reason ?? ""] ?? "No results";
  }
  return STATUS_LABELS[log.status] ?? log.status;
}

function formatDuration(ms: number | null): string {
  if (!ms || ms <= 0) return "—";
  const seconds = ms / 1000;
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds - minutes * 60)}s`;
}

export function ScrapeHealthPanel({ groupId, health }: ScrapeHealthPanelProps) {
  const logsQuery = useQuery({
    queryKey: ["scrape-health-logs", groupId],
    queryFn: () => fetchScrapeLogs({ route_group_id: groupId, limit: 200 }),
    refetchInterval: 30_000,
  });

  const summary = useMemo(() => {
    const logs = logsQuery.data ?? [];
    if (logs.length === 0) return null;

    const durations = logs
      .map((log) => log.duration_ms)
      .filter((value): value is number => typeof value === "number" && value > 0);
    const avgMs = durations.length
      ? durations.reduce((total, value) => total + value, 0) / durations.length
      : null;
    const maxMs = durations.length ? Math.max(...durations) : null;

    const saved = logs.filter((log) => log.status === "success").length;
    const errors = logs.filter(
      (log) => log.status !== "success" && log.status !== "no_results",
    ).length;

    const byOutcome = new Map<string, number>();
    for (const log of logs) {
      const label = outcomeLabel(log);
      byOutcome.set(label, (byOutcome.get(label) ?? 0) + 1);
    }
    const outcomes = [...byOutcome.entries()].sort((a, b) => b[1] - a[1]);

    return { total: logs.length, saved, errors, avgMs, maxMs, outcomes };
  }, [logsQuery.data]);

  const lastHourOk = health ? health.successes_last_hour : 0;
  const lastHourErrors = health ? health.errors_last_hour : 0;
  const healthy = health ? health.status === "ok" || health.status === "never_scraped" : true;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="rounded-2xl border border-slate-100 bg-slate-50/70 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-400">
            {healthy ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 text-red-500" />
            )}
            Status
          </p>
          <p className="mt-1 text-sm font-semibold text-slate-800">
            {health ? (healthy ? "Healthy" : STATUS_LABELS[health.status] ?? health.status) : "—"}
          </p>
          <p className="mt-0.5 text-[11px] text-slate-400">
            {formatFreshnessLabel(health?.last_attempt_at ?? null)}
          </p>
        </div>

        <div className="rounded-2xl border border-slate-100 bg-slate-50/70 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-400">
            <Activity className="h-3.5 w-3.5 text-slate-400" />
            Last hour
          </p>
          <p className="mt-1 text-sm font-semibold text-slate-800">
            {lastHourOk} ok{" "}
            <span className={lastHourErrors > 0 ? "text-red-500" : "text-slate-400"}>
              / {lastHourErrors} errors
            </span>
          </p>
          <p className="mt-0.5 text-[11px] text-slate-400">scheduled + manual attempts</p>
        </div>

        <div className="rounded-2xl border border-slate-100 bg-slate-50/70 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-400">
            <Clock className="h-3.5 w-3.5 text-slate-400" />
            Avg scrape time
          </p>
          <p className="mt-1 text-sm font-semibold text-slate-800">
            {summary ? formatDuration(summary.avgMs) : "—"}
          </p>
          <p className="mt-0.5 text-[11px] text-slate-400">
            slowest {summary ? formatDuration(summary.maxMs) : "—"} (last{" "}
            {summary?.total ?? 0} attempts)
          </p>
        </div>

        <div className="rounded-2xl border border-slate-100 bg-slate-50/70 p-3">
          <p className="text-xs font-medium uppercase tracking-wider text-slate-400">
            Recent attempts
          </p>
          <p className="mt-1 text-sm font-semibold text-slate-800">
            {summary ? `${summary.saved} saved` : "—"}
            {summary && summary.errors > 0 ? (
              <span className="text-red-500"> · {summary.errors} errors</span>
            ) : null}
          </p>
          <p className="mt-0.5 text-[11px] text-slate-400">of last {summary?.total ?? 0} logged</p>
        </div>
      </div>

      {logsQuery.isLoading ? (
        <Skeleton className="h-8" />
      ) : summary && summary.outcomes.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          {summary.outcomes.map(([label, count]) => (
            <span
              key={label}
              className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600"
            >
              {label} <span className="font-semibold text-slate-800">{count}</span>
            </span>
          ))}
        </div>
      ) : (
        <p className="text-xs text-slate-400">No scrape attempts logged yet for this group.</p>
      )}

      {health?.last_error_message ? (
        <p className="rounded-xl border border-red-100 bg-red-50 px-3 py-2 text-xs text-red-600">
          Last error: {shortenError(health.last_error_message)}
        </p>
      ) : null}
    </div>
  );
}
