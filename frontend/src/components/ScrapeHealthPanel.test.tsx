import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import * as collectionApi from "../api/collection";
import { ScrapeHealthPanel } from "./ScrapeHealthPanel";

describe("ScrapeHealthPanel", () => {
  it("renders non-technical wording for legacy raw ROTATION_FAILED JSON errors", async () => {
    vi.spyOn(collectionApi, "fetchScrapeLogs").mockResolvedValue([]);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <ScrapeHealthPanel
          groupId="group-1"
          health={{
            status: "provider_error",
            last_attempt_at: "2026-11-28T08:53:00Z",
            last_success_at: null,
            last_error_message:
              '{"URL":"https://www.kayak.co.uk/i/api/search/dynamic/flights/poll","StatusCode":502,"ErrorCode":90,"ErrorType":"ROTATION_FAILED","Message":["failed"]}',
            errors_last_hour: 13,
            successes_last_hour: 0,
          }}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Connection issue")).toBeTruthy();
    expect(
      screen.getByText(/Temporary proxy connection issue — will retry automatically\./),
    ).toBeTruthy();
  });
});
