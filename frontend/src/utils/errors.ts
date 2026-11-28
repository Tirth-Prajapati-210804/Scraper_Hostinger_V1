const RUN_ERROR_LABELS: Record<string, string> = {
  missing_fares: "{n} dates had no matching flights yet — will retry automatically.",
  restarted_mid_collection: "Server restarted mid-run — automatic recovery started.",
  superseded_by_recovery: "Replaced by an automatic recovery run.",
  provider_unavailable: "No flight data provider is configured.",
  group_not_found: "Route group was not found or is paused.",
  collection_error: "Collection hit a temporary issue — will retry.",
};

export function formatScrapeError(message: string | null | undefined): string {
  if (!message) return "";
  const lowered = message.toLowerCase();

  if (
    lowered.includes("rotation_failed") ||
    lowered.includes('"statuscode":502') ||
    lowered.includes('"errorcode":90') ||
    lowered.includes("bad gateway") ||
    lowered.includes("http 502") ||
    lowered.includes("http 503") ||
    lowered.includes("http 504") ||
    lowered.includes("proxy connection issue") ||
    lowered.includes("connecterror")
  ) {
    return "Temporary proxy connection issue — will retry automatically.";
  }

  if (
    lowered.includes("did not complete") ||
    lowered.includes("first-phase") ||
    lowered.includes("second-phase") ||
    lowered.includes("finish loading flights")
  ) {
    return "KAYAK took too long to finish loading flights — will retry.";
  }

  if (
    lowered.includes("security-check") ||
    lowered.includes("security check") ||
    lowered.includes("real kayak user")
  ) {
    return "KAYAK asked for a security check — will retry on the next run.";
  }

  if (lowered.includes("rate limit") || lowered.includes("too many requests") || lowered.includes("429")) {
    return "Too many requests sent at once — slowing down and retrying.";
  }

  if (lowered.includes("quota") || lowered.includes("credit")) {
    return "Provider credits ran out — top up credits to continue.";
  }

  if (lowered.includes("api key") || lowered.includes("invalid token") || lowered.includes("unauthorized")) {
    return "Provider API key was rejected — check your settings.";
  }

  if (lowered.includes("error with your request") || lowered.includes("you will not be charged")) {
    return "KAYAK page did not render in time — will retry.";
  }

  if (lowered.includes("timed out") || lowered.includes("timeout")) {
    return "KAYAK took too long to respond — will retry.";
  }

  if (lowered.includes("invalid json") || lowered.includes("could not read")) {
    return "Received an unreadable response from KAYAK — will retry.";
  }

  const trimmed = message.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return "Temporary flight lookup issue — will retry automatically.";
  }
  return trimmed.length > 140 ? `${trimmed.slice(0, 140)}…` : trimmed;
}

export function formatRunError(error: unknown): { label: string; detail: string } {
  if (typeof error === "string") {
    return { label: formatScrapeError(error), detail: error };
  }
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
    return { label: formatScrapeError(detail), detail };
  }
  const s = String(error ?? "Unknown error");
  return { label: s, detail: s };
}
