import {
  AlertTriangle,
  History,
  Square,
} from "lucide-react";
import { useState } from "react";

// Pill-style status badge per run status.
const STATUS_STYLES: Record<string, { label: string; cls: string }> = {
  completed: { label: "Completed", cls: "bg-emerald-50 text-emerald-700" },
  partial: { label: "Partial", cls: "bg-amber-50 text-amber-700" },
  stopped: { label: "Stopped", cls: "bg-slate-100 text-slate-600" },
  failed: { label: "Failed", cls: "bg-red-50 text-red-600" },
  running: { label: "Running", cls: "bg-brand-50 text-brand-700" },
};

function StatusPill({ status }: { status: string }) {
  const s = STATUS_STYLES[status] ?? { label: status, cls: "bg-slate-100 text-slate-600" };
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-[3px] text-xs font-medium ${s.cls}`}>
      {status === "running" ? <span className="h-[5px] w-[5px] animate-pulse rounded-full bg-brand-500" /> : null}
      {s.label}
    </span>
  );
}
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
              <th className="px-5 py-3 text-right">Successful</th>
              <th className="px-5 py-3 text-right">Missing / Errors</th>
              {hasRunning && onStop ? <th className="px-5 py-3" /> : null}
            </tr>
          </thead>
          <tbody>
            {runs.map((run, index) => {
              const hasErrorDetail = !!(run.errors && run.errors.length > 0);
              // Missing/errored searches: prefer the failed-route count, but if a
              // run reported issues (e.g. missing-fare dates) without a failed
              // count, surface the number of reported issues so it's not "0".
              const failed = run.routes_failed || (hasErrorDetail ? run.errors!.length : 0);
              const expanded = expandedId === run.id;
              return (
                <tr
                  key={run.id}
                  className={`border-b border-slate-100 align-top ${index % 2 !== 0 ? "bg-slate-50/50" : ""}`}
                >
                  <td className="px-5 py-3 text-slate-600">
                    {formatRelativeTime(run.started_at)}
                  </td>
                  <td className="px-5 py-3 text-slate-600">
                    {formatDuration(run.started_at, run.finished_at)}
                  </td>
                  <td className="px-5 py-3">
                    <StatusPill status={run.status} />
                  </td>
                  {/* Successful searches = data collected for that date. */}
                  <td className="px-5 py-3 text-right">
                    <span className="font-semibold text-green-600">
                      {run.routes_success.toLocaleString()}
                    </span>
                    <span className="text-slate-400"> / {run.routes_total.toLocaleString()}</span>
                  </td>
                  {/* Errored searches / missing dates. Click to see what failed. */}
                  <td className="px-5 py-3 text-right">
                    {failed > 0 || hasErrorDetail ? (
                      <div className="inline-flex flex-col items-end">
                        <button
                          onClick={() => setExpandedId(expanded ? null : run.id)}
                          aria-expanded={expanded}
                          disabled={!hasErrorDetail}
                          className={`inline-flex items-center gap-1 font-semibold text-red-600 ${
                            hasErrorDetail ? "hover:text-red-800" : "cursor-default"
                          }`}
                        >
                          {hasErrorDetail ? <AlertTriangle className="h-3.5 w-3.5" /> : null}
                          {failed.toLocaleString()}
                        </button>
                        {expanded && hasErrorDetail ? (
                          <ul className="mt-1 space-y-0.5 text-right">
                            {run.errors!.map((error, errorIndex) => {
                              const { label, detail } = formatRunError(error);
                              return (
                                <li
                                  key={errorIndex}
                                  title={detail}
                                  className="text-xs font-normal text-red-700"
                                >
                                  {label}
                                </li>
                              );
                            })}
                          </ul>
                        ) : null}
                      </div>
                    ) : (
                      <span className="text-slate-300">0</span>
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
