import { describe, it, expect } from "vitest";
import {
  formatDisplayDate,
  formatDisplayDateTime,
  formatFreshnessLabel,
  formatNumber,
  formatPercent,
  formatRelativeTime,
} from "./format";

describe("formatNumber", () => {
  it("formats zero", () => {
    expect(formatNumber(0)).toBe("0");
  });

  it("formats small numbers without separators", () => {
    expect(formatNumber(999)).toBe("999");
  });

  it("formats large numbers with locale separators", () => {
    const result = formatNumber(1_000_000);
    expect(result).toContain("1");
    expect(result).toContain("000");
  });
});

describe("date and time formatting", () => {
  it("formats date-only values as dd-MM-yyyy", () => {
    expect(formatDisplayDate("2026-06-01")).toBe("01-06-2026");
  });

  it("returns exact local date-time instead of relative text", () => {
    const raw = "2026-06-01T13:05:00";
    expect(formatDisplayDateTime(raw)).toBe("01-06-2026 01:05 PM");
    expect(formatRelativeTime(raw)).toBe("01-06-2026 01:05 PM");
  });

  it("formats scraped labels with exact time", () => {
    expect(formatFreshnessLabel("2026-06-01T01:09:00")).toBe("Scraped 01-06-2026 01:09 AM");
  });

  it("handles empty dates", () => {
    expect(formatDisplayDate(null)).toBe("-");
    expect(formatDisplayDateTime(undefined)).toBe("Never");
    expect(formatFreshnessLabel(null)).toBe("Not scraped yet");
  });
});

describe("formatPercent", () => {
  it("formats to 1 decimal place", () => {
    expect(formatPercent(75.5)).toBe("75.5%");
    expect(formatPercent(100)).toBe("100.0%");
    expect(formatPercent(0)).toBe("0.0%");
  });
});
