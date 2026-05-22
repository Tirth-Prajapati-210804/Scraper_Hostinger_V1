import {
  AlertTriangle,
  CheckCircle,
  History,
  Loader2,
  Square,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import type { CollectionRun } from "../types/price";
import { formatRelativeTime } from "../utils/format";
import { Skeleton } from "./ui/Skeleton";

function formatDuration(startedAt: string, finishedAt: string | null): string {
  if (!finishedAt) return "-";
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  const totalSec = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSec / 60);
  const seconds = totalSec % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function formatRunError(error: unknown): string {
  if (typeof error === "string") return error;
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    const code = typeof record.code === "string" ? record.code : "collection_error";
    const detail = typeof record.detail === "string" ? record.detail : JSON.stringify(record);
    return `${code}: ${detail}`;
  }
  return String(error ?? "Unknown error");
}

function formatPauseReason(reason: string | null | undefined): string {
  switch (reason) {
    case "duration_retry_exhausted":
      return "Duration fallback exhausted";
    case "operational_retry_exhausted":
      return "Deferred operational retry exhausted";
    case "repeated_operational_failures":
      return "Repeated operational failures";
    default:
      return reason ?? "Automatic safeguard";
  }
}

interface CollectionRunsTableProps {
  runs: CollectionRun[];
  isLoading: boolean;
  onStop?: () => void;
  stopping?: boolean;
}

export function CollectionRunsTable({
  runs,
  isLoading,
  onStop,
  stopping,
}: CollectionRunsTableProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (isLoading) {
    return <Skeleton className="h-48 rounded-xl" />;
  }

  if (!runs.length) {
    return (
      <div className="flex flex-col items-center gap-2 py-12 text-slate-400">
        <History className="h-8 w-8 text-slate-300" />
        <p className="text-sm font-medium">No collection runs yet</p>
        <p className="text-xs">Trigger a collection from the dashboard to get started.</p>
      </div>
    );
  }

  const hasRunning = runs.some((run) => run.status === "running");

  return (
    <div className="overflow-hidden rounded-[16px] border border-slate-200">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wider text-slate-500">
              <th className="px-5 py-3">Started</th>
              <th className="px-5 py-3">Duration</th>
              <th className="px-5 py-3">Status</th>
              <th className="px-5 py-3">Routes</th>
              <th className="px-5 py-3 text-right">Prices</th>
              <th className="px-5 py-3">Errors</th>
              {hasRunning && onStop ? <th className="px-5 py-3" /> : null}
            </tr>
          </thead>
          <tbody>
            {runs.map((run, index) => {
              const primarySafeguard = run.safeguards?.[0];
              const hasDetails =
                (run.errors && run.errors.length > 0) ||
                primarySafeguard?.auto_pause_triggered ||
                (primarySafeguard?.deferred_duration_dates ?? 0) > 0 ||
                (primarySafeguard?.deferred_operational_dates ?? 0) > 0;

              return (
                <tr
                  key={run.id}
                  className={`border-b border-slate-100 ${index % 2 !== 0 ? "bg-slate-50/50" : ""}`}
                >
                <td className="px-5 py-3 text-slate-600">
                  {formatRelativeTime(run.started_at)}
                </td>
                <td className="px-5 py-3 text-slate-600">
                  {formatDuration(run.started_at, run.finished_at)}
                </td>
                <td className="px-5 py-3">
                  {run.status === "completed" ? (
                    <span className="flex items-center gap-1 text-green-600">
                      <CheckCircle className="h-3.5 w-3.5" /> done
                    </span>
                  ) : run.status === "failed" ? (
                    <span className="flex items-center gap-1 text-red-500">
                      <XCircle className="h-3.5 w-3.5" /> failed
                    </span>
                  ) : run.status === "stopped" ? (
                    <span className="flex items-center gap-1 text-amber-500">
                      <Square className="h-3.5 w-3.5" /> stopped
                    </span>
                  ) : run.status === "partial" ? (
                    <span className="flex items-center gap-1 text-amber-600">
                      <AlertTriangle className="h-3.5 w-3.5" /> partial
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-brand-600">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> running
                    </span>
                  )}
                </td>
                <td className="px-5 py-3 text-slate-700">
                  {run.routes_success}/{run.routes_total}
                </td>
                <td className="px-5 py-3 text-right text-slate-700">
                  {run.dates_scraped.toLocaleString()}
                </td>
                <td className="px-5 py-3">
                  {hasDetails ? (
                    <div>
                      <button
                        onClick={() => setExpandedId(expandedId === run.id ? null : run.id)}
                        aria-expanded={expandedId === run.id}
                        className={`flex items-center gap-1 text-xs ${
                          run.status === "partial"
                            ? "text-amber-600 hover:text-amber-800"
                            : "text-red-600 hover:text-red-800"
                        }`}
                      >
                        <AlertTriangle className="h-3.5 w-3.5" />
                        {primarySafeguard?.auto_pause_triggered
                          ? formatPauseReason(primarySafeguard.auto_pause_reason)
                          : run.status === "partial"
                            ? "missing fare dates"
                            : `${run.errors?.length ?? 0} route${(run.errors?.length ?? 0) > 1 ? "s" : ""}`}
                      </button>
                      {expandedId === run.id ? (
                        <ul className="mt-1 space-y-0.5">
                          {primarySafeguard ? (
                            <>
                              <li className="text-xs text-slate-700">
                                Outcome: <span className="font-medium">{primarySafeguard.group_run_outcome}</span>
                              </li>
                              <li className="text-xs text-slate-700">
                                Failure streak:{" "}
                                <span className="font-medium">
                                  {primarySafeguard.consecutive_operational_failures}
                                </span>
                              </li>
                              {primarySafeguard.deferred_duration_dates ? (
                                <li className="text-xs text-slate-700">
                                  Deferred to next duration fallback run:{" "}
                                  <span className="font-medium">{primarySafeguard.deferred_duration_dates}</span>
                                </li>
                              ) : null}
                              {primarySafeguard.deferred_operational_dates ? (
                                <li className="text-xs text-slate-700">
                                  Deferred to next retry run:{" "}
                                  <span className="font-medium">{primarySafeguard.deferred_operational_dates}</span>
                                </li>
                              ) : null}
                              {primarySafeguard.auto_pause_note ? (
                                <li className="text-xs text-slate-700">{primarySafeguard.auto_pause_note}</li>
                              ) : null}
                            </>
                          ) : null}
                          {(run.errors ?? []).map((error, errorIndex) => (
                            <li
                              key={errorIndex}
                              className={`font-mono text-xs ${
                                run.status === "partial" ? "text-amber-700" : "text-red-700"
                              }`}
                            >
                              {formatRunError(error)}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  ) : (
                    <span className="text-xs text-slate-400">-</span>
                  )}
                </td>
                {hasRunning && onStop ? (
                  <td className="px-5 py-3 text-right">
                    {run.status === "running" ? (
                      <button
                        onClick={onStop}
                        disabled={stopping}
                        className="inline-flex items-center gap-1 rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
                      >
                        <Square className="h-3 w-3" />
                        {stopping ? "Stopping..." : "Stop"}
                      </button>
                    ) : null}
                  </td>
                ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
