import { describe, expect, it } from "vitest";

import { STOP_MODE_OPTIONS, formatStopModeLabel, stopModeToApi, stopModeToUi } from "./stopModes";

describe("stopModes", () => {
  it("exposes the exact stop-mode labels shown in the UI", () => {
    expect(STOP_MODE_OPTIONS.map((option) => option.label)).toEqual([
      "Direct",
      "1 Stop",
      "2 Stop",
      "Prefer 1 Stop",
      "Prefer 2 Stop",
    ]);
  });

  it("maps backend values to the correct route-form option ids", () => {
    expect(stopModeToUi(0)).toBe("direct");
    expect(stopModeToUi(1)).toBe("1-stop");
    expect(stopModeToUi(2)).toBe("2-stop");
    expect(stopModeToUi(3)).toBe("prefer-1");
    expect(stopModeToUi(4)).toBe("prefer-2");
    expect(stopModeToUi(null)).toBe("prefer-1");
  });

  it("maps route-form option ids back to backend stop-mode values", () => {
    expect(stopModeToApi("direct")).toBe(0);
    expect(stopModeToApi("1-stop")).toBe(1);
    expect(stopModeToApi("2-stop")).toBe(2);
    expect(stopModeToApi("prefer-1")).toBe(3);
    expect(stopModeToApi("prefer-2")).toBe(4);
    expect(stopModeToApi("unknown")).toBe(3);
  });

  it("formats saved route groups with the new labels", () => {
    expect(formatStopModeLabel(0)).toBe("Direct");
    expect(formatStopModeLabel(1)).toBe("1 Stop");
    expect(formatStopModeLabel(2)).toBe("2 Stop");
    expect(formatStopModeLabel(3)).toBe("Prefer 1 Stop");
    expect(formatStopModeLabel(4)).toBe("Prefer 2 Stop");
    expect(formatStopModeLabel(null)).toBe("Prefer 1 Stop");
  });
});
