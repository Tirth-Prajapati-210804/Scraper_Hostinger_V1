export const STOP_MODE_OPTIONS = [
  { id: "direct", label: "Direct", value: 0 },
  { id: "1-stop", label: "1 Stop", value: 1 },
  { id: "2-stop", label: "2 Stop", value: 2 },
] as const;

export type StopModeId = (typeof STOP_MODE_OPTIONS)[number]["id"];

export function stopModeToUi(value: number | null | undefined): StopModeId {
  return STOP_MODE_OPTIONS.find((option) => option.value === value)?.id ?? "1-stop";
}

export function stopModeToApi(value: string): number {
  return STOP_MODE_OPTIONS.find((option) => option.id === value)?.value ?? 1;
}

export function formatStopModeLabel(value: number | null | undefined): string {
  return STOP_MODE_OPTIONS.find((option) => option.value === value)?.label ?? "1 Stop";
}
