export const STOP_MODE_OPTIONS = [
  { id: "direct", label: "Direct", value: 0 },
  { id: "1-stop", label: "1 Stop", value: 1 },
  { id: "2-stop", label: "2 Stop", value: 2 },
  { id: "prefer-1", label: "Prefer 1 Stop", value: 3 },
  { id: "prefer-2", label: "Prefer 2 Stop", value: 4 },
] as const;

export type StopModeId = (typeof STOP_MODE_OPTIONS)[number]["id"];

export function stopModeToUi(value: number | null | undefined): StopModeId {
  return STOP_MODE_OPTIONS.find((option) => option.value === value)?.id ?? "prefer-1";
}

export function stopModeToApi(value: string): number {
  return STOP_MODE_OPTIONS.find((option) => option.id === value)?.value ?? 3;
}

export function formatStopModeLabel(value: number | null | undefined): string {
  return STOP_MODE_OPTIONS.find((option) => option.value === value)?.label ?? "Prefer 1 Stop";
}
