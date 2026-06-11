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

// Short, plain-English label per known run-error code. Keeps the run table clean
// instead of dumping the long backend `detail`. The FULL detail is preserved and
// shown on hover (title=) so nothing is lost. {n} is filled from the detail's count.
const RUN_ERROR_LABELS: Record<string, string> = {
  missing_fares: "{n} dates had no fare after filtering — will retry.",
  restarted_mid_collection: "Server restarted mid-run — recovery started.",
  superseded_by_recovery: "Superseded by an automatic recovery run.",
  provider_unavailable: "No flight data provider available.",
  group_not_found: "Route group not found or inactive.",
  collection_error: "Collection error — will retry.",
};

// Returns {label, detail}: label = short text shown inline; detail = full original
// text shown on hover. label preserves key specifics (e.g. the date count).
function formatRunError(error: unknown): { label: string; detail: string } {
  if (typeof error === "string") return { label: error, detail: error };
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    const code = typeof record.code === "string" ? record.code : "collection_error";
    const detail = typeof record.detail === "string" ? record.detail : JSON.stringify(record);
    let label = RUN_ERROR_LABELS[code];
    if (label) {
      if (label.includes("{n}")) {
        const m = detail.match(/\d[\d,]*/);
        label = label.replace("{n}", m ? m[0] : "Some");
      }
      return { label, detail: `${code}: ${detail}` };
    }
    return { label: detail.length > 120 ? `${detail.slice(0, 120)}…` : detail, detail };
  }
  const s = String(error ?? "Unknown error");
  return { label: s, detail: s };
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
            {runs.map((run, index) => (
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
                  {run.errors && run.errors.length > 0 ? (
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
                        {run.status === "partial"
                          ? "missing fare dates"
                          : `${run.errors.length} route${run.errors.length > 1 ? "s" : ""}`}
                      </button>
                      {expandedId === run.id ? (
                        <ul className="mt-1 space-y-0.5">
                          {run.errors.map((error, errorIndex) => {
                            const { label, detail } = formatRunError(error);
                            return (
                              <li
                                key={errorIndex}
                                title={detail}
                                className={`text-xs ${
                                  run.status === "partial" ? "text-amber-700" : "text-red-700"
                                }`}
                              >
                                {label}
                              </li>
                            );
                          })}
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
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
