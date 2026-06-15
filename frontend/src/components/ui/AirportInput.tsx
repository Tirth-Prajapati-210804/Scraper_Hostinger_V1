import { useEffect, useState } from "react";

import { fetchLocationSuggestions } from "../../api/locations";
import type { LocationSuggestion } from "../../types/location";

interface AirportInputProps {
  /** The current airport/metro code (single value). Empty string = unset. */
  value: string;
  onChange: (code: string) => void;
  placeholder?: string;
  /** Allow an empty value (e.g. the last leg's "To = back to origin"). */
  allowEmpty?: boolean;
}

const IATA_RE = /^[A-Za-z0-9]{2,4}$/;

function suggestionHint(suggestion: LocationSuggestion): string {
  switch (suggestion.kind) {
    case "airport_code":
      return "Use code";
    case "airport":
      return "Single airport";
    case "country":
      return "Country — picks primary airport";
    default:
      return "All airports (metro)";
  }
}

/**
 * Single-airport-code field with the same location autocomplete as TagInput,
 * but it commits ONE code to a string (not a list of chips). Used for the
 * multi-city leg From/To fields, where each leg is a single flight. A matched
 * city offers the metro "all airports" code AND each member airport, so the
 * user can pick either.
 */
export function AirportInput({ value, onChange, placeholder, allowEmpty = false }: AirportInputProps) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<LocationSuggestion[]>([]);
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const trimmed = value.trim();
  const showSuggestions = open && trimmed.length > 0 && suggestions.length > 0;

  useEffect(() => {
    if (!open || trimmed.length === 0) {
      setSuggestions([]);
      setHighlightedIndex(0);
      return;
    }
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        setSuggestions(await fetchLocationSuggestions(trimmed));
        setHighlightedIndex(0);
      } catch {
        setSuggestions([]);
      } finally {
        setLoading(false);
      }
    }, 150);
    return () => window.clearTimeout(timer);
  }, [open, trimmed]);

  function pick(suggestion: LocationSuggestion) {
    // A leg is one flight from one place, so take the suggestion's primary
    // code (the metro code for a city/country row, or the airport's own code).
    const code = suggestion.codes[0]?.trim().toUpperCase() ?? "";
    if (code) {
      onChange(code);
    }
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (showSuggestions && e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightedIndex((current) => Math.min(current + 1, suggestions.length - 1));
      return;
    }
    if (showSuggestions && e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightedIndex((current) => Math.max(current - 1, 0));
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      if (showSuggestions && suggestions[highlightedIndex]) {
        e.preventDefault();
        pick(suggestions[highlightedIndex]);
        return;
      }
      setOpen(false);
      return;
    }
    if (e.key === "Escape") {
      setOpen(false);
    }
  }

  const isInvalid = trimmed.length > 0 && !IATA_RE.test(trimmed);

  return (
    <div className="relative">
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value.toUpperCase());
          setOpen(true);
        }}
        onKeyDown={handleKeyDown}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        placeholder={placeholder}
        className={`h-11 w-full rounded-[10px] border bg-white px-3 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 ${
          isInvalid
            ? "border-red-400 ring-4 ring-red-100"
            : "border-slate-200 hover:border-slate-300 focus:border-brand-500"
        }`}
      />

      {showSuggestions ? (
        <div className="absolute left-0 right-0 top-[calc(100%+4px)] z-20 max-h-72 overflow-auto rounded-[12px] border border-slate-200 bg-white p-1 shadow-[0_18px_50px_-38px_rgba(15,23,42,0.45)]">
          {suggestions.map((suggestion, index) => (
            <button
              key={`${suggestion.kind}-${suggestion.label}-${suggestion.codes[0] ?? ""}`}
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pick(suggestion)}
              className={`flex w-full items-start justify-between gap-3 rounded-[10px] px-3 py-2 text-left transition ${
                index === highlightedIndex ? "bg-brand-50" : "hover:bg-slate-50"
              }`}
            >
              <div>
                <div className="text-sm font-medium text-slate-900">{suggestion.label}</div>
                <div className="mt-0.5 text-xs text-slate-400">{suggestionHint(suggestion)}</div>
              </div>
              <div className="text-xs font-medium text-slate-500">
                {suggestion.codes.slice(0, 3).join(", ")}
                {suggestion.codes.length > 3 ? ` +${suggestion.codes.length - 3}` : ""}
              </div>
            </button>
          ))}
        </div>
      ) : null}

      {loading && open ? (
        <p className="mt-1 text-[11px] text-slate-400">Searching locations…</p>
      ) : isInvalid ? (
        <p className="mt-1 text-[11px] text-red-500">Enter a Kayak code (2–4 letters/digits).</p>
      ) : allowEmpty && trimmed.length === 0 ? (
        <p className="mt-1 text-[11px] text-slate-400">Leave blank to fly back to the group origin.</p>
      ) : null}
    </div>
  );
}
