import { describe, expect, it } from "vitest";

import { formatRunError, formatScrapeError } from "./errors";

describe("formatScrapeError", () => {
  it("translates raw Scrape.do 502 ROTATION_FAILED JSON into plain English", () => {
    const raw =
      '{"URL":"https://www.kayak.co.uk/i/api/search/dynamic/flights/poll","StatusCode":502,"ErrorCode":90,"ErrorType":"ROTATION_FAILED","Message":["..."]}';
    expect(formatScrapeError(raw)).toBe(
      "Temporary proxy connection issue — will retry automatically.",
    );
  });

  it("translates incomplete KAYAK poll errors into plain English", () => {
    expect(formatScrapeError("KAYAK poll did not complete after 1 polls (status=second-phase).")).toBe(
      "KAYAK took too long to finish loading flights — will retry.",
    );
  });
});

describe("formatRunError", () => {
  it("translates missing_fares run errors without raw snake_case code prefix", () => {
    const res = formatRunError({
      code: "missing_fares",
      detail: "26 date/destination check(s) returned no valid fare after filtering and still need collection.",
    });
    expect(res.label).toBe("26 dates had no matching flights yet — will retry automatically.");
    expect(res.detail).toContain("missing_fares:");
  });
});
