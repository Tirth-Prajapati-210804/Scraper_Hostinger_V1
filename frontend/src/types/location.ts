export interface LocationSuggestion {
  label: string;
  codes: string[];
  kind: "country" | "city" | "airport" | "airport_code";
}
