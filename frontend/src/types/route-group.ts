export type TripType = "round_trip" | "multi_city";
export type RouteMarket = string;

export interface RouteGroup {
  id: string;
  name: string;
  destination_label: string;
  destinations: string[];
  origins: string[];
  nights: number;
  days_ahead: number;
  trip_type: TripType;
  sheet_name_map: Record<string, string>;
  special_sheets: SpecialSheet[];
  multi_city_legs: MultiCityLegConfig[] | null;
  is_active: boolean;
  market: RouteMarket;
  currency: string;
  max_stops: number | null;
  same_airline_only: boolean;
  max_leg_duration_minutes: number | null;
  max_layover_minutes: number | null;
  start_date: string | null;
  end_date: string | null;
  created_at: string;
  updated_at: string;
}

export interface SpecialSheet {
  name: string;
  origin: string;
  destination_label: string;
  destinations: string[];
  columns: number;
}

/** One EXTRA leg of a multi-city itinerary (beyond the first leg).
 *  destination "" on the LAST leg = back to the group origin.
 *  nights_before = nights at the previous stop (0 = fly out the next day). */
export interface MultiCityLegConfig {
  origin: string;
  destination: string;
  nights_before: number;
}

export type ScrapeHealthStatus =
  | "ok"
  | "never_scraped"
  | "rate_limited"
  | "quota_exhausted"
  | "auth_error"
  | "parse_error"
  | "provider_error"
  | "stopped";

export interface ScrapeHealth {
  status: ScrapeHealthStatus;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error_message: string | null;
  errors_last_hour: number;
  successes_last_hour: number;
}

export type DateStatusKind = "no_fare" | "empty" | "error";

export interface DateStatusSummary {
  status: DateStatusKind | string;
  attempts: number;
}

export interface RouteGroupProgress {
  route_group_id: string;
  name: string;
  total_dates: number;
  dates_with_data: number;
  coverage_percent: number;
  last_scraped_at: string | null;
  per_origin: Record<string, { total: number; collected: number }>;
  scraped_dates: string[];
  /** Attempted-but-uncollected dates: ISO date -> why it is blank. */
  date_statuses?: Record<string, DateStatusSummary>;
  health: ScrapeHealth;
}
