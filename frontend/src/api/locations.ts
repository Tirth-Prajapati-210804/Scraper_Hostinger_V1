import { api } from "./client";
import type { LocationSuggestion } from "../types/location";

export async function fetchLocationSuggestions(
  query: string,
  limit = 20,
): Promise<LocationSuggestion[]> {
  const res = await api.get<LocationSuggestion[]>("/api/v1/route-groups/location-suggestions", {
    params: { q: query, limit },
  });
  return res.data;
}
