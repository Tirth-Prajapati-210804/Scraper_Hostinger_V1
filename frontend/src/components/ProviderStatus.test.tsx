import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ProviderStatus } from "./ProviderStatus";
import * as statsApi from "../api/stats";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("ProviderStatus", () => {
  it("renders Scrape.do label and live Scrape.do credit balance", async () => {
    vi.spyOn(statsApi, "fetchProviderCredits").mockResolvedValue({
      available: true,
      provider: "scrapedo",
      max_credits: 10000,
      used_credits: 690,
      remaining_credits: 9310,
    });

    renderWithQuery(
      <ProviderStatus
        health={{
          status: "ok",
          environment: "development",
          database_status: "ok",
          scheduler_running: true,
          provider_status: {
            scrapedo: "configured",
            scrapingbee: "disabled",
          },
        }}
      />,
    );

    expect(screen.getByText("Scrape.do")).toBeTruthy();
    expect(screen.getByText("ScrapingBee")).toBeTruthy();
    expect(await screen.findByText("690")).toBeTruthy();
    expect(screen.getByText(/10,000/)).toBeTruthy();
  });
});
