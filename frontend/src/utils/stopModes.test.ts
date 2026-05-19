import { describe, expect, it } from "vitest";

import { STOP_MODE_OPTIONS, formatStopModeLabel, stopModeToApi, stopModeToUi } from "./stopModes";

describe("stopModes", () => {
  it("exposes the exact stop-mode labels shown in the UI", () => {
    expect(STOP_MODE_OPTIONS.map((option) => option.label)).toEqual([
      "Direct",
      "1 Stop",
      "2 Stop",
    ]);
  });

  it("maps backend values to the correct route-form option ids", () => {
    expect(stopModeToUi(0)).toBe("direct");
    expect(stopModeToUi(1)).toBe("1-stop");
    expect(stopModeToUi(2)).toBe("2-stop");
    expect(stopModeToUi(null)).toBe("1-stop");
  });

  it("maps route-form option ids back to backend stop-mode values", () => {
    expect(stopModeToApi("direct")).toBe(0);
    expect(stopModeToApi("1-stop")).toBe(1);
    expect(stopModeToApi("2-stop")).toBe(2);
    expect(stopModeToApi("unknown")).toBe(1);
  });

  it("formats saved route groups with the new labels", () => {
    expect(formatStopModeLabel(0)).toBe("Direct");
    expect(formatStopModeLabel(1)).toBe("1 Stop");
    expect(formatStopModeLabel(2)).toBe("2 Stop");
    expect(formatStopModeLabel(null)).toBe("1 Stop");
  });
});
