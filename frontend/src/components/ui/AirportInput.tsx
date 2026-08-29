import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

import { fetchLocationSuggestions } from "../../api/locations";
import type { LocationSuggestion } from "../../types/location";

interface AirportInputProps {
  /** The current airport/metro code (single value). Empty string = unset. */
  value: string;
  onChange: (code: string) => void;
  placeholder?: string;
  /** Allow an empty value (e.g. the last leg's "To = back to origin"). */
  allowEmpty?: boolean;
  /** Accept a comma-joined list of ALTERNATIVE airports ("ASJ,SES"): each code
   *  is validated separately and the scraper searches every combination,
   *  keeping the cheapest. Autocomplete applies to the part being typed. */
  multi?: boolean;
}

const IATA_RE = /^[A-Za-z0-9]{2,4}$/;

/**
 * Single-airport-code field with the same location autocomplete as TagInput,
 * but it commits ONE code to a string (not a list of chips). Used for the
 * multi-city leg From/To fields, where each leg is a single flight. A matched
 * city offers the metro "all airports" code AND each member airport, so the
 * user can pick either.
 */
export function AirportInput({ value, onChange, placeholder, allowEmpty = false, multi = false }: AirportInputProps) {
  const [draft, setDraft] = useState("");
  const [invalid, setInvalid] = useState(false);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<LocationSuggestion[]>([]);
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  // Committed chips = every code already in `value`. In single mode there is
  // at most one. The draft (uncommitted, currently-typed) segment lives in
  // local state and is what autocomplete searches against.
  const chips = value
    .split(",")
    .map((part) => part.trim().toUpperCase())
    .filter(Boolean);
  const trimmedDraft = draft.trim();
  const showSuggestions = open && trimmedDraft.length > 0 && suggestions.length > 0;

  useEffect(() => {
    if (!open || trimmedDraft.length === 0) {
      setSuggestions([]);
      setHighlightedIndex(0);
      return;
    }
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        setSuggestions(await fetchLocationSuggestions(trimmedDraft));
        setHighlightedIndex(0);
      } catch {
        setSuggestions([]);
      } finally {
        setLoading(false);
      }
    }, 150);
    return () => window.clearTimeout(timer);
  }, [open, trimmedDraft]);

  function commit(codes: string[]) {
    if (multi) {
      const merged = [...chips];
      codes.forEach((code) => {
        if (code && !merged.includes(code)) merged.push(code);
      });
      onChange(merged.join(","));
    } else {
      onChange(codes[0] ?? "");
    }
    setDraft("");
  }

  function tryAddDraft() {
    const code = trimmedDraft.toUpperCase();
    if (!code) return;
    if (!IATA_RE.test(code)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    commit([code]);
  }

  function pick(suggestion: LocationSuggestion) {
    setInvalid(false);
    commit(suggestion.codes.map((code) => code.trim().toUpperCase()).filter(Boolean));
    setOpen(false);
  }

  function removeChip(code: string) {
    onChange(chips.filter((c) => c !== code).join(","));
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
    if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
      if (showSuggestions && suggestions[highlightedIndex]) {
        e.preventDefault();
        pick(suggestions[highlightedIndex]);
        return;
      }
      if (!multi && chips.length > 0) {
        // Single mode replaces the one committed chip rather than stacking.
        e.preventDefault();
        onChange("");
        tryAddDraft();
        return;
      }
      e.preventDefault();
      tryAddDraft();
      return;
    }
    if (e.key === "Backspace" && draft === "" && chips.length > 0) {
      removeChip(chips[chips.length - 1]);
      return;
    }
    if (e.key === "Escape") {
      setOpen(false);
    }
    setInvalid(false);
  }

  function handleBlur() {
    window.setTimeout(() => setOpen(false), 120);
    if (trimmedDraft && !showSuggestions) {
      tryAddDraft();
    }
  }

  return (
    <div className="relative">
      <div
        ref={containerRef}
        onClick={() => containerRef.current?.querySelector("input")?.focus()}
        className={`flex min-h-[38px] w-full cursor-text flex-wrap items-center gap-1.5 rounded-[8px] border bg-white px-3 py-1.5 transition ${
          invalid
            ? "border-red-400 ring-4 ring-red-100"
            : "border-[#DCE3EC] hover:border-slate-300 focus-within:border-brand-500"
        }`}
      >
        {chips.map((code) => (
          <span
            key={code}
            className="inline-flex items-center gap-1 rounded-full bg-brand-50 px-2.5 py-1 text-xs font-semibold text-brand-700"
          >
            {code}
            <button
              type="button"
              aria-label={`Remove ${code}`}
              onClick={(e) => {
                e.stopPropagation();
                removeChip(code);
              }}
              className="rounded-full p-0.5 text-brand-500 transition hover:bg-white hover:text-brand-800"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}

        {multi || chips.length === 0 ? (
          <input
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setInvalid(false);
              setOpen(true);
            }}
            onKeyDown={handleKeyDown}
            onBlur={handleBlur}
            onFocus={() => setOpen(true)}
            placeholder={chips.length === 0 ? placeholder : ""}
            className="h-6 min-w-[90px] flex-1 border-0 bg-transparent p-0 text-sm text-slate-900 outline-none ring-0 focus:border-0 focus:outline-none focus:ring-0 placeholder:text-slate-400"
          />
        ) : null}
      </div>

      {showSuggestions ? (
        <div className="absolute left-0 right-0 top-[calc(100%+4px)] z-20 max-h-72 overflow-auto rounded-[8px] border border-[#DCE3EC] bg-white p-1 shadow-[0_18px_50px_-38px_rgba(15,23,42,0.45)]">
          {suggestions.map((suggestion, index) => (
            <button
              key={`${suggestion.kind}-${suggestion.label}-${suggestion.codes[0] ?? ""}`}
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pick(suggestion)}
              className={`flex w-full items-center justify-between gap-3 rounded-[6px] px-3 py-2 text-left transition ${
                index === highlightedIndex ? "bg-brand-50" : "hover:bg-slate-50"
              }`}
            >
              <span className="text-sm font-medium text-slate-900">{suggestion.label}</span>
              <span className="text-xs font-medium text-slate-500">
                {suggestion.codes.slice(0, 3).join(", ")}
                {suggestion.codes.length > 3 ? ` +${suggestion.codes.length - 3}` : ""}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {loading && open ? (
        <p className="mt-1 text-[11px] text-slate-400">Searching…</p>
      ) : invalid ? (
        <p className="mt-1 text-[11px] text-red-500">Enter a valid airport or city code.</p>
      ) : allowEmpty && chips.length === 0 ? (
        <p className="mt-1 text-[11px] text-slate-400">Blank flies back to the group origin.</p>
      ) : null}
    </div>
  );
}
