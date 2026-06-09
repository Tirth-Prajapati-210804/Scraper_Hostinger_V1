import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRight,
  CircleHelp,
  Minus,
  Plane,
  Plus,
  Search,
} from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useMemo, useState } from "react";

import { getErrorMessage } from "../api/client";
import { createRouteGroup, updateRouteGroup } from "../api/route-groups";
import { useToast } from "../context/ToastContext";
import type { RouteGroup, RouteMarket, SpecialSheet, TripType } from "../types/route-group";
import { STOP_MODE_OPTIONS, stopModeToApi, stopModeToUi, type StopModeId } from "../utils/stopModes";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Modal";
import { Select } from "./ui/Select";
import { TagInput } from "./ui/TagInput";

interface RouteGroupFormProps {
  open: boolean;
  onClose: () => void;
  initial?: RouteGroup | null;
}

type UiTripType = "roundtrip" | "multicity";

interface ManualLeg {
  from: string[];
  to: string[];
}

interface ManualState {
  tripType: UiTripType;
  groupName: string;
  outboundLabel: string;
  mainLeg: ManualLeg;
  returnLeg: ManualLeg;
  nights: string;
  days: string;
  market: RouteMarket;
  currency: string;
  startDate: string;
  endDate: string;
  stops: StopModeId;
  maxLayoverHours: string;
  maxLegDurationHours: string;
  isActive: boolean;
}

const CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "SGD", "AED", "INR"];
const MARKETS: Array<{ value: RouteMarket; label: string }> = [
  { value: "us", label: "US - United States" },
  { value: "ca", label: "CA - Canada" },
  { value: "uk", label: "UK - United Kingdom" },
  { value: "in", label: "IN - India" },
  { value: "au", label: "AU - Australia" },
  { value: "ie", label: "IE - Ireland" },
  { value: "de", label: "DE - Germany" },
  { value: "fr", label: "FR - France" },
  { value: "es", label: "ES - Spain" },
  { value: "it", label: "IT - Italy" },
  { value: "nl", label: "NL - Netherlands" },
  { value: "ch", label: "CH - Switzerland" },
  { value: "se", label: "SE - Sweden" },
  { value: "no", label: "NO - Norway" },
  { value: "dk", label: "DK - Denmark" },
  { value: "fi", label: "FI - Finland" },
  { value: "jp", label: "JP - Japan" },
  { value: "sg", label: "SG - Singapore" },
  { value: "ae", label: "AE - United Arab Emirates" },
  { value: "mx", label: "MX - Mexico" },
  { value: "nz", label: "NZ - New Zealand" },
  { value: "br", label: "BR - Brazil" },
  { value: "za", label: "ZA - South Africa" },
];

const TRIP_TYPES: Array<{
  id: UiTripType;
  label: string;
  description: string;
}> = [
  { id: "roundtrip", label: "Round Trip", description: "Outbound + auto-return" },
  { id: "multicity", label: "Multi-city", description: "Open-jaw itinerary" },
];
const MAX_LEG_DURATION_OPTIONS = [
  { label: "Any", value: "" },
  { label: "8h", value: "8" },
  { label: "12h", value: "12" },
  { label: "16h", value: "16" },
  { label: "24h", value: "24" },
  { label: "36h", value: "36" },
];
const MAX_LAYOVER_OPTIONS = [
  { label: "Any", value: "" },
  { label: "6h", value: "6" },
  { label: "8h", value: "8" },
  { label: "11h", value: "11" },
  { label: "16h", value: "16" },
];

function tripTypeToUi(type?: TripType | null): UiTripType {
  if (type === "multi_city") return "multicity";
  return "roundtrip";
}

function tripTypeToApi(type: UiTripType): TripType {
  if (type === "multicity") return "multi_city";
  return "round_trip";
}

function parsePositiveInt(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function toIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function todayIso(): string {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return toIsoDate(today);
}

function addDaysIso(isoDate: string, days: number): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(year, (month ?? 1) - 1, day ?? 1);
  date.setDate(date.getDate() + days);
  return toIsoDate(date);
}

function inclusiveDayCount(startDate: string, endDate: string): number | null {
  if (!startDate || !endDate) return null;
  const start = new Date(`${startDate}T00:00:00`);
  const end = new Date(`${endDate}T00:00:00`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end < start) {
    return null;
  }
  return Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;
}

function normalizeCodes(values: string[]) {
  return values.map((item) => item.trim().toUpperCase()).filter(Boolean);
}

function deriveName(origins: string[], destinations: string[]) {
  return `${origins.join(", ")} to ${destinations.join(", ")}`;
}

function buildInitialManualState(initial?: RouteGroup | null): ManualState {
  const tripType = tripTypeToUi(initial?.trip_type);
  const returnSheet = initial?.special_sheets[0];

  return {
    tripType,
    groupName: initial?.name ?? "",
    outboundLabel: initial?.destination_label ?? "",
    mainLeg: {
      from: initial?.origins ?? [],
      to: initial?.destinations ?? [],
    },
    returnLeg: {
      from: returnSheet ? [returnSheet.origin] : [],
      to: returnSheet?.destinations ?? initial?.origins ?? [],
    },
    nights: String(initial?.nights ?? 10),
    days: String(initial?.days_ahead ?? 365),
    market: initial?.market ?? "us",
    currency: initial?.currency ?? "USD",
    startDate: initial?.start_date ?? "",
    endDate: initial?.end_date ?? "",
    stops: stopModeToUi(initial?.max_stops),
    maxLayoverHours: initial?.max_layover_minutes
      ? String(Math.round(initial.max_layover_minutes / 60))
      : "",
    maxLegDurationHours: initial?.max_leg_duration_minutes
      ? String(Math.round(initial.max_leg_duration_minutes / 60))
      : "",
    isActive: initial?.is_active ?? true,
  };
}

function FieldLabel({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="mb-2 flex items-center gap-1.5 text-[13px] font-medium text-[#3f4e6e]">
      <span>{children}</span>
      {hint ? (
        <span title={hint} className="text-[#97a4bb]">
          <CircleHelp className="h-3.5 w-3.5" />
        </span>
      ) : null}
    </div>
  );
}

function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`h-[50px] w-full rounded-2xl border border-[#dfe6f0] bg-white px-4 text-[15px] text-[#0f172a] outline-none transition placeholder:text-[#9ba8bf] focus:border-brand-500 ${props.className ?? ""}`}
    />
  );
}

function SelectInput(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <Select
      value={props.value as string}
      disabled={props.disabled}
      onChange={(event) => props.onChange?.(event as unknown as React.ChangeEvent<HTMLSelectElement>)}
      className={`h-[50px] border-[#dfe6f0] px-4 text-[15px] text-[#0f172a] focus:border-brand-500 ${props.className ?? ""}`}
    >
      {props.children}
    </Select>
  );
}

function StepperField({
  label,
  value,
  onChange,
  min = 1,
  max,
  suffix,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  min?: number;
  max?: number;
  suffix?: string;
}) {
  const numeric = parsePositiveInt(value, min);
  const bounded = max ? Math.min(numeric, max) : numeric;

  return (
    <div>
      <FieldLabel>{label}</FieldLabel>
      <div className="flex h-[50px] items-center overflow-hidden rounded-2xl border border-[#dfe6f0] bg-white">
        <button
          type="button"
          onClick={() => onChange(String(Math.max(min, numeric - 1)))}
          className="flex h-full w-12 items-center justify-center text-[#6b7a93] transition hover:bg-slate-50"
        >
          <Minus className="h-4 w-4" />
        </button>
        <div className="flex flex-1 items-center justify-center gap-1 text-[16px] font-semibold text-[#0f172a]">
          <input
            type="text"
            inputMode="numeric"
            value={value}
            onChange={(event) => onChange(event.target.value.replace(/\D/g, ""))}
            onBlur={() => onChange(String(bounded))}
            className="w-20 border-0 bg-transparent text-center text-[16px] font-semibold text-[#0f172a] outline-none ring-0 focus:border-0 focus:outline-none focus:ring-0"
          />
          {suffix ? <span className="text-[13px] font-medium text-[#7b89a3]">{suffix}</span> : null}
        </div>
        <button
          type="button"
          onClick={() => onChange(String(max ? Math.min(max, numeric + 1) : numeric + 1))}
          className="flex h-full w-12 items-center justify-center text-[#6b7a93] transition hover:bg-slate-50"
        >
          <Plus className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

function TripTypeSelector({
  value,
  onChange,
}: {
  value: UiTripType;
  onChange: (value: UiTripType) => void;
}) {
  return (
    <div className="space-y-3">
      <FieldLabel>Trip Type</FieldLabel>
      <div className="grid gap-3 sm:grid-cols-3">
        {TRIP_TYPES.map((type) => {
          const active = value === type.id;
          return (
            <button
              key={type.id}
              type="button"
              onClick={() => onChange(type.id)}
              className={`rounded-2xl border px-4 py-4 text-left transition ${
                active
                  ? "border-brand-500 bg-[#edf2ff] text-brand-700 shadow-[0_18px_38px_-32px_rgba(59,130,246,0.55)]"
                  : "border-[#e1e8f1] bg-white text-[#12203f] hover:border-[#ccd8ea]"
              }`}
            >
              <div className="text-[16px] font-semibold">{type.label}</div>
              <div className="mt-1 text-[13px] text-[#7c8aa5]">{type.description}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function AirportField({
  label,
  value,
  onChange,
  placeholder,
  hint,
}: {
  label: string;
  value: string[];
  onChange: (tags: string[]) => void;
  placeholder: string;
  hint: string;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <FieldLabel>{label}</FieldLabel>
        <span className="text-[12px] font-medium text-brand-600">Add multiple</span>
      </div>
      <div className="relative">
        <Search className="pointer-events-none absolute left-4 top-4 h-4 w-4 text-[#a0aec4]" />
        <TagInput
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          hint={hint}
          className="pl-9"
        />
      </div>
    </div>
  );
}

function RoutePanel({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[24px] border border-[#e4ebf4] bg-white p-5 shadow-[0_24px_60px_-48px_rgba(15,23,42,0.28)]">
      <div className="mb-5 flex items-center gap-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[#edf2ff] text-brand-600">
          <Plane className="h-4 w-4" />
        </div>
        <h3 className="text-[18px] font-semibold text-[#111b37]">{title}</h3>
      </div>
      <div className="space-y-5">{children}</div>
    </section>
  );
}

function ConnectionSelector({
  value,
  onChange,
}: {
  value: StopModeId;
  onChange: (value: StopModeId) => void;
}) {
  return (
    <div>
      <FieldLabel hint="Use an exact stop count to control which itineraries are collected.">
        Connections
      </FieldLabel>
      <div className="flex flex-wrap gap-2">
        {STOP_MODE_OPTIONS.map((option) => {
          const active = value === option.id;
          return (
            <button
              key={option.id}
              type="button"
              onClick={() => onChange(option.id)}
              className={`rounded-2xl border px-4 py-2.5 text-[14px] font-medium transition ${
                active
                  ? "border-brand-500 bg-[#edf2ff] text-brand-700"
                  : "border-[#dfe6f0] bg-white text-[#52637f] hover:border-[#cad5e7]"
              }`}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function SameAirlineNotice() {
  return (
    <div className="rounded-[24px] border border-[#dfe6f0] bg-[#f8fbff] px-5 py-4 text-[14px] text-[#47556f]">
      <span>
        <span className="block font-medium text-[#12203f]">Same airline only</span>
        <span className="mt-1 block text-[13px] leading-6 text-[#7b8aa4]">
          Same-airline collection is always enabled. Only itineraries where the outbound and return
          airline match will be saved, and the cheapest valid same-airline result is used.
        </span>
      </span>
    </div>
  );
}

function MaxLayoverSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <FieldLabel hint="Caps the halt/layover time at each stop. A halt longer than this is excluded as impractical. Applied by Kayak before results load.">
        Max Layover
      </FieldLabel>
      <div className="flex flex-wrap items-center gap-2">
        {MAX_LAYOVER_OPTIONS.map((option) => {
          const active = value === option.value;
          return (
            <button
              key={option.label}
              type="button"
              onClick={() => onChange(option.value)}
              className={`h-10 rounded-2xl border px-4 text-[14px] font-medium transition ${
                active
                  ? "border-brand-500 bg-[#edf2ff] text-brand-700"
                  : "border-[#dfe6f0] bg-white text-[#52637f] hover:border-[#cad5e7]"
              }`}
            >
              {option.label}
            </button>
          );
        })}
        <input
          type="text"
          inputMode="numeric"
          value={value}
          onChange={(event) => onChange(event.target.value.replace(/\D/g, ""))}
          placeholder="Custom hours"
          className="h-10 w-36 rounded-2xl border border-[#dfe6f0] bg-white px-4 text-[14px] text-[#0f172a] outline-none placeholder:text-[#9ba8bf] focus:border-brand-500"
        />
      </div>
    </div>
  );
}

function MaxLegDurationSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <FieldLabel hint="Filters each flight leg independently. Round trips are not added together.">
        Max Leg Duration
      </FieldLabel>
      <div className="flex flex-wrap items-center gap-2">
        {MAX_LEG_DURATION_OPTIONS.map((option) => {
          const active = value === option.value;
          return (
            <button
              key={option.label}
              type="button"
              onClick={() => onChange(option.value)}
              className={`h-10 rounded-2xl border px-4 text-[14px] font-medium transition ${
                active
                  ? "border-brand-500 bg-[#edf2ff] text-brand-700"
                  : "border-[#dfe6f0] bg-white text-[#52637f] hover:border-[#cad5e7]"
              }`}
            >
              {option.label}
            </button>
          );
        })}
        <input
          type="text"
          inputMode="numeric"
          value={value}
          onChange={(event) => onChange(event.target.value.replace(/\D/g, ""))}
          placeholder="Custom hours"
          className="h-10 w-36 rounded-2xl border border-[#dfe6f0] bg-white px-4 text-[14px] text-[#0f172a] outline-none placeholder:text-[#9ba8bf] focus:border-brand-500"
        />
      </div>
    </div>
  );
}

export function RouteGroupForm({ open, onClose, initial }: RouteGroupFormProps) {
  const qc = useQueryClient();
  const { showToast } = useToast();
  const [state, setState] = useState<ManualState>(() => buildInitialManualState(initial));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setState(buildInitialManualState(initial));
    setError("");
  }, [initial, open]);

  useEffect(() => {
    if (state.tripType !== "multicity") return;

    setState((current) => ({
      ...current,
      returnLeg: {
        from: current.returnLeg.from,
        to: current.mainLeg.from,
      },
      stops: current.stops,
    }));
  }, [state.tripType, state.mainLeg.from]);

  const isEditing = Boolean(initial);
  const normalizedOrigins = useMemo(() => normalizeCodes(state.mainLeg.from), [state.mainLeg.from]);
  const normalizedDestinations = useMemo(() => normalizeCodes(state.mainLeg.to), [state.mainLeg.to]);

  async function refreshQueries(groupId?: string) {
    await qc.invalidateQueries({ queryKey: ["route-groups"] });
    if (groupId) {
      await qc.invalidateQueries({ queryKey: ["route-group", groupId] });
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");

    try {
      const mainOrigins = normalizeCodes(state.mainLeg.from);
      const mainDestinations = normalizeCodes(state.mainLeg.to);

      if (!mainOrigins.length || !mainDestinations.length) {
        throw new Error("Add at least one origin and one destination in the outbound leg.");
      }

      const specialSheets: SpecialSheet[] = [];
      const resolvedDays = parsePositiveInt(state.days, 365);
      const resolvedStartDate = state.startDate || todayIso();
      const resolvedEndDate = state.endDate || addDaysIso(resolvedStartDate, resolvedDays - 1);
      const resolvedDayCount = inclusiveDayCount(resolvedStartDate, resolvedEndDate) ?? resolvedDays;
      const maxLegDurationHours = Number.parseInt(state.maxLegDurationHours, 10);
      const maxLegDurationMinutes =
        Number.isFinite(maxLegDurationHours) && maxLegDurationHours > 0
          ? Math.min(maxLegDurationHours, 48) * 60
          : null;
      const maxLayoverHours = Number.parseInt(state.maxLayoverHours, 10);
      const maxLayoverMinutes =
        Number.isFinite(maxLayoverHours) && maxLayoverHours > 0
          ? Math.min(maxLayoverHours, 48) * 60
          : null;

      if (state.tripType === "multicity") {
        const returnOrigins = normalizeCodes(state.returnLeg.from);
        if (!returnOrigins.length) {
          throw new Error("Add at least one return origin for the multi-city route.");
        }

        specialSheets.push({
          name: "Return Leg",
          origin: returnOrigins[0],
          destination_label: state.outboundLabel.trim() || mainOrigins.join("/"),
          destinations: mainOrigins,
          columns: 4,
        });
      }

      const payload = {
        name: state.groupName.trim() || deriveName(mainOrigins, mainDestinations),
        destination_label: state.outboundLabel.trim() || mainDestinations.join("/"),
        origins: mainOrigins,
        destinations: mainDestinations,
        nights: parsePositiveInt(state.nights, 10),
        days_ahead: resolvedDayCount,
        sheet_name_map: Object.fromEntries(mainOrigins.map((origin) => [origin, origin])),
        special_sheets: specialSheets,
        market: state.market,
        currency: state.currency,
        max_stops: stopModeToApi(state.stops),
        same_airline_only: true,
        max_leg_duration_minutes: maxLegDurationMinutes,
        max_layover_minutes: maxLayoverMinutes,
        start_date: resolvedStartDate,
        end_date: resolvedEndDate,
        trip_type: tripTypeToApi(state.tripType),
        ...(isEditing ? { is_active: state.isActive } : {}),
      };

      if (initial) {
        await updateRouteGroup(initial.id, payload);
        await refreshQueries(initial.id);
        showToast("Route group saved", "success");
      } else {
        const created = await createRouteGroup(payload);
        await refreshQueries(created.id);
        showToast(`Created: ${created.name}`, "success");
      }

      onClose();
    } catch (err) {
      setError(getErrorMessage(err, "Failed to save route group."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={isEditing ? "Edit Route Group" : "Create Route Group"}
      eyebrow="Route Groups"
      size="xl"
      className="max-w-[1120px] rounded-[30px] border-[#e5ebf3] shadow-[0_40px_120px_-70px_rgba(15,23,42,0.45)]"
      headerClassName="px-7 pb-4 pt-6"
      bodyClassName="px-7 pb-7 pt-6"
      titleClassName="text-[22px] font-bold text-[#111b37]"
      closeButtonClassName="h-11 w-11 rounded-2xl border-[#dfe6f0] text-[#60708d]"
    >
      <form onSubmit={handleSubmit} className="space-y-6">
        <p className="-mt-2 text-[15px] text-[#6f809f]">
          Configure a new route group for price collection.
        </p>

        <div className="grid gap-5 md:grid-cols-2">
          <div>
            <FieldLabel>Route Group Name</FieldLabel>
            <TextInput
              value={state.groupName}
              onChange={(e) => setState((current) => ({ ...current, groupName: e.target.value }))}
              placeholder="e.g. Europe Routes"
            />
            <p className="mt-2 text-[12px] text-[#92a0b7]">
              A descriptive name for this route group.
            </p>
          </div>

          <div>
            <FieldLabel>Data Provider</FieldLabel>
            <SelectInput value="scrapingbee" disabled>
              <option value="scrapingbee">Scrapingbee</option>
            </SelectInput>
            <p className="mt-2 text-[12px] text-[#92a0b7]">
              The provider that will fetch the data.
            </p>
          </div>
        </div>

        <TripTypeSelector
          value={state.tripType}
          onChange={(tripType) => setState((current) => ({ ...current, tripType }))}
        />

        <div className="grid gap-5 xl:grid-cols-2">
          <RoutePanel title="Outbound Leg">
            <AirportField
              label="From (Origin Airport)"
              value={state.mainLeg.from}
              onChange={(tags) => setState((current) => ({ ...current, mainLeg: { ...current.mainLeg, from: tags } }))}
              placeholder="Search origin airport..."
              hint="Use Enter, comma, or a location suggestion."
            />
            <AirportField
              label="To (Destination Airport)"
              value={state.mainLeg.to}
              onChange={(tags) => setState((current) => ({ ...current, mainLeg: { ...current.mainLeg, to: tags } }))}
              placeholder="Search destination airport..."
              hint="Use Enter, comma, or a location suggestion."
            />
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <FieldLabel>Destination Label</FieldLabel>
                <TextInput
                  value={state.outboundLabel}
                  onChange={(e) => setState((current) => ({ ...current, outboundLabel: e.target.value }))}
                  placeholder="e.g. London"
                />
              </div>
              <StepperField
                label="Nights at destination"
                value={state.nights}
                onChange={(nights) => setState((current) => ({ ...current, nights }))}
              />
            </div>
          </RoutePanel>

          {state.tripType === "roundtrip" ? (
            <RoutePanel title="Return Leg">
              <div className="space-y-4 rounded-[20px] border border-[#e3eaf4] bg-[#f8fbff] p-5">
                <div className="grid gap-4 md:grid-cols-[1fr_auto_1fr] md:items-center">
                  <div className="rounded-2xl border border-[#dfe6f0] bg-white px-4 py-3 text-[14px] text-[#47556f]">
                    {normalizedDestinations.length ? normalizedDestinations.join(", ") : "Outbound destination"}
                  </div>
                  <div className="flex justify-center text-[#a0aec4]">
                    <ArrowLeftRight className="h-4 w-4" />
                  </div>
                  <div className="rounded-2xl border border-[#dfe6f0] bg-white px-4 py-3 text-[14px] text-[#47556f]">
                    {normalizedOrigins.length ? normalizedOrigins.join(", ") : "Outbound origin"}
                  </div>
                </div>
                <label className="flex items-start gap-3 rounded-2xl bg-white px-4 py-3 text-[14px] text-[#47556f]">
                  <input
                    type="checkbox"
                    checked
                    readOnly
                    className="mt-0.5 h-4 w-4 rounded border-[#c7d2e4] text-brand-600"
                  />
                  <span>
                    <span className="block font-medium text-[#12203f]">Auto-generate return from outbound</span>
                    <span className="mt-1 block text-[13px] text-[#7b8aa4]">
                      Return airports are mirrored automatically using the outbound route.
                    </span>
                  </span>
                </label>
              </div>
            </RoutePanel>
          ) : (
            <RoutePanel title="Return Leg">
              <AirportField
                label="From (Origin Airport)"
                value={state.returnLeg.from}
                onChange={(tags) =>
                  setState((current) => ({
                    ...current,
                    returnLeg: {
                      ...current.returnLeg,
                      from: tags,
                      to: current.mainLeg.from,
                    },
                  }))
                }
                placeholder="Search return origin airport..."
                hint="Where the itinerary returns from after the stay."
              />

              <div>
                <div className="mb-2 flex items-center justify-between">
                  <FieldLabel>To (Destination Airport)</FieldLabel>
                  <span className="text-[12px] font-medium text-brand-600">Auto linked</span>
                </div>
                <div className="rounded-2xl border border-[#dfe6f0] bg-[#f8fbff] px-4 py-[14px] text-[15px] text-[#47556f]">
                  {normalizedOrigins.length ? normalizedOrigins.join(", ") : "Original outbound origin"}
                </div>
                <p className="mt-2 text-[12px] text-[#92a0b7]">
                  Multi-city return always connects back to the outbound origin.
                </p>
              </div>
            </RoutePanel>
          )}
        </div>

        <div className="grid gap-5 xl:grid-cols-[0.9fr_1.2fr_1fr_1fr]">
          <StepperField
            label="Booking Window (Days)"
            value={state.days}
            max={730}
            onChange={(days) =>
              setState((current) => {
                const parsedDays = parsePositiveInt(days, 1);
                const startDate = current.startDate || todayIso();
                return {
                  ...current,
                  days,
                  startDate,
                  endDate: addDaysIso(startDate, Math.min(parsedDays, 730) - 1),
                };
              })
            }
          />

          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <FieldLabel>Travel Window From</FieldLabel>
              <TextInput
                type="date"
                value={state.startDate}
                onChange={(e) =>
                  setState((current) => {
                    const startDate = e.target.value;
                    if (!startDate) {
                      return { ...current, startDate: "", endDate: "" };
                    }
                    const parsedDays = parsePositiveInt(current.days, 1);
                    const endDate = current.endDate && current.endDate >= startDate
                      ? current.endDate
                      : addDaysIso(startDate, parsedDays - 1);
                    const dayCount = inclusiveDayCount(startDate, endDate);
                    return {
                      ...current,
                      startDate,
                      endDate,
                      days: dayCount ? String(Math.min(dayCount, 730)) : current.days,
                    };
                  })
                }
              />
            </div>
            <div>
              <FieldLabel>Travel Window To</FieldLabel>
              <TextInput
                type="date"
                value={state.endDate}
                onChange={(e) =>
                  setState((current) => {
                    const endDate = e.target.value;
                    if (!endDate) {
                      return { ...current, endDate: "" };
                    }
                    const startDate = current.startDate || todayIso();
                    const dayCount = inclusiveDayCount(startDate, endDate);
                    return {
                      ...current,
                      startDate,
                      endDate,
                      days: dayCount ? String(Math.min(dayCount, 730)) : current.days,
                    };
                  })
                }
              />
            </div>
          </div>

          <div>
            <FieldLabel>Market</FieldLabel>
            <SelectInput
              value={state.market}
              onChange={(e) => setState((current) => ({ ...current, market: e.target.value as RouteMarket }))}
            >
              {MARKETS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </SelectInput>
          </div>

          <div>
            <FieldLabel>Currency</FieldLabel>
            <SelectInput
              value={state.currency}
              onChange={(e) => setState((current) => ({ ...current, currency: e.target.value }))}
            >
              {CURRENCIES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </SelectInput>
          </div>
        </div>

        <ConnectionSelector
          value={state.stops}
          onChange={(stops) => setState((current) => ({ ...current, stops }))}
        />

        <SameAirlineNotice />

        <MaxLayoverSelector
          value={state.maxLayoverHours}
          onChange={(maxLayoverHours) => setState((current) => ({ ...current, maxLayoverHours }))}
        />

        <MaxLegDurationSelector
          value={state.maxLegDurationHours}
          onChange={(maxLegDurationHours) => setState((current) => ({ ...current, maxLegDurationHours }))}
        />

        {isEditing ? (
          <label className="flex items-center gap-3 rounded-2xl border border-[#dfe6f0] bg-[#f8fbff] px-4 py-3 text-[14px] text-[#47556f]">
            <input
              type="checkbox"
              checked={state.isActive}
              onChange={(e) => setState((current) => ({ ...current, isActive: e.target.checked }))}
              className="h-4 w-4 rounded border-[#c7d2e4] text-brand-600"
            />
            Keep this route group active
          </label>
        ) : null}

        {error ? (
          <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
            {error}
          </div>
        ) : null}

        <div className="flex flex-col gap-3 border-t border-[#e8edf5] pt-5 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[13px] text-[#92a0b7]">
            {state.tripType === "multicity"
              ? "One matching multi-city itinerary is saved per date using the cheapest valid fare."
              : "Airport codes can be added manually or chosen from the location suggestions."}
          </p>
          <div className="flex gap-3 self-end">
            <Button
              type="button"
              variant="secondary"
              onClick={onClose}
              className="h-12 rounded-2xl px-6 text-[15px]"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              loading={saving}
              className="h-12 rounded-2xl px-6 text-[15px] font-semibold shadow-[0_18px_44px_-30px_rgba(37,99,235,0.8)]"
            >
              <Plus className="h-4 w-4" />
              {isEditing ? "Save Route Group" : "Create Route Group"}
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
