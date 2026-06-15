import type { HealthResponse, OverviewStats, ProviderCredits } from "../types/stats";
import { api } from "./client";

export async function fetchOverviewStats(): Promise<OverviewStats> {
  const res = await api.get<OverviewStats>("/api/v1/stats/overview");
  return res.data;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await api.get<HealthResponse>("/health");
  return res.data;
}

export async function fetchProviderCredits(): Promise<ProviderCredits> {
  const res = await api.get<ProviderCredits>("/api/v1/stats/provider-credits");
  return res.data;
}
