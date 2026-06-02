# Design: Probe-then-skip for non-collectable route groups

Status: **design only — not yet built.** Build after the timeout / no-results / accuracy commits are shipped and verified in production.

## Problem

Some airport pairs (e.g. CIA = Rome Ciampino → IAD transatlantic same-airline) have **no flight inventory on Kayak**. Today the scraper attempts every date in the group's range, and each empty date burns a full render budget (~85s) plus ScrapingBee credit before recording "no fare". A fully-dead route wastes the whole collection cycle one expensive empty date at a time. Observed: of 4 groups created, 3 failed purely because the airport pair has no Kayak data.

## Why a naive manual pre-check is not enough (client's objection)

Checking only 1-2 **near** dates can produce a false negative: a route may have **no near-term** data but **some far-future** data (or vice versa). Skipping a group on a couple of near samples could discard dates that genuinely have fares. The probe must sample **across the whole horizon**, not just the start.

## Design: probe-then-decide (per group, at collection start)

When a collection is triggered for a group, run a small **probe phase** before the full date sweep:

1. **Pick representative probe dates** spread across the group's date range:
   - early window (first ~month),
   - middle window (mid horizon),
   - late window (near the end / `days_ahead`).
   - ~3 dates per window = ~9 probes total (tunable; start with 3 windows × 1-3 dates).
   - Reuse `_group_dates(group)` and sample indices at ~10%, ~50%, ~90% of the range (plus optional neighbours).
2. **Scrape only those probe dates** through the normal provider path. Each returns one of:
   - `success` (fares found) → route is **live**.
   - `no_results` (Kayak explicitly empty — uses the `np`/`no_results` detector already added) → that date is empty.
   - `page_empty` / `extract_failed` / timeout → inconclusive (treat as not-proven-dead).
3. **Decide:**
   - If **any** probe returns `success` → route is collectable → proceed with the **full** collection normally.
   - If **all** probes return `no_results` (explicit Kayak "no results", not mere render failure) → mark the group **not collectable** and skip the full sweep.
   - If probes are all inconclusive (timeouts/render failures, not explicit no-results) → do **not** mark dead (could be a transient render issue); fall back to current behaviour or retry later. This protects against wrongly skipping a live route during a Kayak/ScrapingBee hiccup.

Key safety rule: **only an explicit Kayak "no results" signal counts as evidence of a dead route** — never a timeout or render failure. This is why the `no_results` detector (already shipped) is a prerequisite.

## Schema change (RouteGroup)

Add two columns (one Alembic migration, both nullable / defaulted so existing rows are unaffected):
- `collectable_status: str` — one of `unknown` (default), `collectable`, `not_collectable`.
- `collectable_reason: str | None` — human message, e.g. "No Kayak inventory found across early/middle/late sample dates on 2026-06-02."
- (optional) `collectable_checked_at: datetime | None` — when the probe last ran, so the status can be re-evaluated periodically rather than being permanent.

`is_active` stays as-is (user on/off). `collectable_status` is system-determined. A `not_collectable` group is skipped by the scheduler but still visible.

## Scheduler hook

In `run_collection_cycle` (all-groups) and `trigger_single_group` (manual), before building `planned_routes`/`planned_segments` for a group:
- if `collectable_status == "not_collectable"` and `collectable_checked_at` is recent → skip the group (don't enqueue its dates).
- else run the probe phase; persist the resulting status + reason; proceed or skip accordingly.

Manual trigger should be able to **force a re-probe** (bypass a stale `not_collectable`), mirroring the existing manual no-fare-skip bypass.

## Dashboard

- Show a clear badge/message on a `not_collectable` group: e.g. "Not collectable — no Kayak inventory found (checked 2026-06-02)."
- Provide a "Re-check" action that forces a re-probe (routes may gain inventory later).

## Cost / accuracy tradeoff

- Probe cost: ~3-9 scrapes to decide a whole group, vs. potentially hundreds of empty-date timeouts. Large net credit saving on dead routes.
- False-negative protection: sampling across the full horizon + requiring explicit `no_results` (not timeouts) avoids wrongly skipping live routes.
- Re-check support: `not_collectable` is not permanent; routes can be re-probed as Kayak inventory changes.

## Prerequisites (must be shipped + verified first)

1. Timeout decouple (`_render_budget_ms`) — so probes fail fast, not at ~133s.
2. `no_results` detector (`f.empty()` / `np`) — the only trustworthy "dead route" signal.
3. (Recommended) the `run_one` never-awaited fix — so skipped/short-circuited groups don't leak coroutines.

## Open questions

- Exact probe count per window (start 3 windows × 1 date = 3 probes; increase if false negatives appear).
- How long a `not_collectable` verdict stays before auto re-probe (e.g. 7-14 days).
- Whether to probe per-segment (multi-city has multiple legs/origins) or per-group.
