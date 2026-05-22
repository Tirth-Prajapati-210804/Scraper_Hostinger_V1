export interface DailyPrice {
  id: string;
  origin: string;
  destination: string;
  depart_date: string; // "2026-04-15"
  airline: string;
  price: number;
  currency: string;
  provider: string;
  stops: number | null;
  stop_label?: string | null;
  duration_minutes: number | null;
  itinerary_data?: {
    duration_text?: string | null;
    leg_durations?: number[] | null;
    legs?: Array<{
      duration_text?: string | null;
      duration_minutes?: number | null;
    }> | null;
  } | null;
  scraped_at: string;
}

export interface PriceTrend {
  date: string; // mapped from depart_date
  price: number;
  airline: string;
}

export interface CollectionRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: "running" | "completed" | "partial" | "failed" | "stopped";
  routes_total: number;
  routes_success: number;
  routes_failed: number;
  dates_scraped: number;
  errors: unknown[] | null;
}

export interface ScrapeLogEntry {
  id: string;
  origin: string;
  destination: string;
  depart_date: string;
  provider: string;
  status:
    | "success"
    | "no_results"
    | "rate_limited"
    | "quota_exhausted"
    | "auth_error"
    | "provider_error"
    | "parse_error"
    | "stopped";
  offers_found: number;
  result_reason:
    | "success"
    | "page_empty"
    | "extract_failed"
    | "filtered_out"
    | "market_mismatch"
    | null;
  raw_offers_found: number;
  eligible_offers_found: number;
  filtered_by_stop_count: number;
  filtered_by_same_airline: number;
  filtered_by_duration: number;
  requested_market: string | null;
  requested_currency: string | null;
  detected_currency: string | null;
  cheapest_price: number | null;
  error_message: string | null;
  duration_ms: number | null;
  created_at: string;
}
