/**
 * DESIGN PREVIEW (scratch) — a STATIC render of the redesigned "Claude design"
 * Create Route Group form, ported from .redesign/app/form-creategroup.jsx into the
 * live design-system kit, WITH the pieces the prototype was missing added in
 * (multi-city Trip Legs panel, Max Layover). It does NOT save anything -- it's for
 * you to open, inspect, and mark where you want changes. Once approved, the real
 * wired RouteGroupForm is rebuilt to match.
 *
 * Route: /design-preview
 */
import { useState } from "react";

import { Btn, Field, Icon, Input, cx } from "../components/ds";

const MARKETS: Array<[string, string]> = [
  ["uk", "United Kingdom"], ["us", "United States"], ["ca", "Canada"], ["in", "India"],
  ["au", "Australia"], ["de", "Germany"], ["fr", "France"], ["es", "Spain"],
];
const CURRENCIES = ["GBP", "USD", "EUR", "CAD", "AUD", "INR", "AED", "SGD"];
const STOPS: Array<[string, string]> = [["direct", "Direct"], ["1-stop", "1 Stop"], ["2-stop", "2 Stop"]];
const LEGDUR: Array<[string, string]> = [["", "Any"], ["8", "8h"], ["12", "12h"], ["16", "16h"], ["24", "24h"]];
const LAYOVER: Array<[string, string]> = [["", "Any"], ["6", "6h"], ["8", "8h"], ["11", "11h"], ["16", "16h"]];

function TagInput({ value, onChange, placeholder }: { value: string[]; onChange: (v: string[]) => void; placeholder?: string }) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim().toUpperCase();
    if (v && !value.includes(v)) onChange([...value, v]);
    setDraft("");
  };
  return (
    <div
      className="ds-row ds-wrap ds-gap-2"
      style={{ minHeight: 38, padding: "5px 8px", border: "1px solid var(--border-strong)", borderRadius: "var(--r-sm)", background: "var(--surface)" }}
    >
      {value.map((t) => (
        <span key={t} className="badge badge--accent" style={{ paddingRight: 5 }}>
          {t}
          <button onClick={() => onChange(value.filter((x) => x !== t))} style={{ display: "flex", border: "none", background: "none", color: "inherit", padding: 0, opacity: 0.7 }}>
            <Icon name="x" size={12} />
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
          else if (e.key === "Backspace" && !draft && value.length) onChange(value.slice(0, -1));
        }}
        onBlur={add}
        placeholder={value.length ? "" : placeholder}
        style={{ flex: 1, minWidth: 90, border: "none", outline: "none", background: "transparent", fontSize: 13, color: "var(--ink)", height: 26 }}
      />
    </div>
  );
}

function Stepper({ value, onChange, min = 1, max = 999, suffix }: { value: string; onChange: (v: string) => void; min?: number; max?: number; suffix?: string }) {
  const n = parseInt(value, 10) || min;
  return (
    <div className="ds-row" style={{ height: 38, border: "1px solid var(--border-strong)", borderRadius: "var(--r-sm)", overflow: "hidden", background: "var(--surface)" }}>
      <button type="button" onClick={() => onChange(String(Math.max(min, n - 1)))} className="ds-row ds-center" style={{ width: 38, height: "100%", border: "none", background: "transparent", color: "var(--text-soft)" }}><Icon name="minus" size={15} /></button>
      <div className="ds-grow ds-row ds-center ds-gap-1" style={{ borderLeft: "1px solid var(--border)", borderRight: "1px solid var(--border)" }}>
        <input type="text" inputMode="numeric" value={value} onChange={(e) => onChange(e.target.value.replace(/\D/g, ""))} style={{ width: 60, border: "none", outline: "none", background: "transparent", textAlign: "center", fontWeight: 600, color: "var(--ink)", fontSize: 14 }} />
        {suffix ? <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 500 }}>{suffix}</span> : null}
      </div>
      <button type="button" onClick={() => onChange(String(Math.min(max, n + 1)))} className="ds-row ds-center" style={{ width: 38, height: "100%", border: "none", background: "transparent", color: "var(--text-soft)" }}><Icon name="plus" size={15} /></button>
    </div>
  );
}

const TRIP_TYPES = [
  { id: "round", icon: "swap", title: "Round trip", desc: "Out and back from the same origin airports." },
  { id: "multi", icon: "layers", title: "Multi-city · open-jaw", desc: "Return from a different airport than you landed at." },
];

function TripType({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="triptype">
      {TRIP_TYPES.map((t) => (
        <button key={t.id} type="button" className={cx("triptype__card", value === t.id && "is-active")} onClick={() => onChange(t.id)}>
          <span className="triptype__icon"><Icon name={t.icon} size={18} /></span>
          <span className="triptype__txt">
            <span className="triptype__title">{t.title}</span>
            <span className="triptype__desc">{t.desc}</span>
          </span>
          <span className="triptype__check"><Icon name="check" size={13} /></span>
        </button>
      ))}
    </div>
  );
}

function FormSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="stack-4" style={{ paddingTop: 20, borderTop: "1px solid var(--border)" }}>
      <div className="eyebrow">{label}</div>
      {children}
    </section>
  );
}

function ChipRow({ options, value, onChange }: { options: Array<[string, string]>; value: string; onChange: (v: string) => void }) {
  return (
    <div className="ds-row ds-wrap ds-gap-2">
      {options.map(([id, label]) => (
        <button key={id} className={cx("choice", value === id && "is-active")} onClick={() => onChange(id)}>{label}</button>
      ))}
    </div>
  );
}

export function DesignPreviewPage() {
  const [trip, setTrip] = useState("multi");
  const [name, setName] = useState("");
  const [from, setFrom] = useState<string[]>(["LON"]);
  const [to, setTo] = useState<string[]>(["KEF"]);
  const [label, setLabel] = useState("");
  const [nights, setNights] = useState("2");
  const [days, setDays] = useState("365");
  const [market, setMarket] = useState("uk");
  const [currency, setCurrency] = useState("GBP");
  const [stops, setStops] = useState("2-stop");
  const [legdur, setLegdur] = useState("");
  const [layover, setLayover] = useState("11");
  const [sameAirline, setSameAirline] = useState(true);
  // Missing-from-prototype: multi-city extra legs (added so you see the COMPLETE form).
  const [extraLegs, setExtraLegs] = useState([
    { from: "KEF", to: "YYZ", nights: "2" },
    { from: "NYC", to: "", nights: "5" },
  ]);

  return (
    <div className="ds-fade-in" style={{ maxWidth: 760, margin: "0 auto", padding: "28px 0" }}>
      <div className="eyebrow" style={{ marginBottom: 4 }}>Design preview (scratch — not wired)</div>
      <h1 className="page-title" style={{ marginBottom: 4 }}>Create Route Group</h1>
      <p className="page-sub" style={{ marginBottom: 20 }}>The redesigned form + the previously-missing controls (multi-city legs, Max Layover). Mark what to change.</p>

      <div className="card stack-5">
        {/* Basics */}
        <div className="stack-4">
          <Field label="Group name" hint="A short, descriptive name. Auto-generated from the route if left blank.">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. UK -> Europe Sun" />
          </Field>
          <Field label="Trip type">
            <TripType value={trip} onChange={setTrip} />
          </Field>
        </div>

        {/* Outbound */}
        <FormSection label="Outbound leg">
          <div className="ds-grid ds-g-2">
            <Field label="From — origin airports" help="Press Enter or comma to add multiple codes.">
              <TagInput value={from} onChange={setFrom} placeholder="Add airport code..." />
            </Field>
            <Field label="To — destination airports">
              <TagInput value={to} onChange={setTo} placeholder="Add airport code..." />
            </Field>
          </div>
          <div className="ds-grid ds-g-2">
            <Field label="Destination label"><Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Reykjavik" /></Field>
            <Field label="Nights at destination"><Stepper value={nights} onChange={setNights} suffix="nights" /></Field>
          </div>
        </FormSection>

        {/* Trip Legs (multi-city) — MISSING from prototype, added here */}
        {trip === "multi" ? (
          <FormSection label={`Trip legs (${extraLegs.length + 1} flights)`}>
            <p className="field__hint" style={{ marginTop: -4 }}>Leg 1 is the outbound above. Each leg departs its own nights after the previous one. Last leg's "To" blank = back to origin.</p>
            {extraLegs.map((leg, i) => (
              <div key={i} className="stack-3" style={{ border: "1px solid var(--border)", borderRadius: "var(--r-md)", background: "var(--surface-soft)", padding: 14 }}>
                <div className="ds-row ds-between">
                  <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-soft)" }}>Leg {i + 2}</span>
                  {extraLegs.length > 1 ? (
                    <button onClick={() => setExtraLegs(extraLegs.filter((_, j) => j !== i))} style={{ border: "none", background: "none", color: "var(--danger)", fontSize: 12, fontWeight: 500, cursor: "pointer" }}>Remove</button>
                  ) : null}
                </div>
                <div className="ds-grid ds-g-3">
                  <Field label="From"><Input value={leg.from} onChange={(e) => setExtraLegs(extraLegs.map((l, j) => j === i ? { ...l, from: e.target.value.toUpperCase() } : l))} placeholder="e.g. KEF" /></Field>
                  <Field label={i === extraLegs.length - 1 ? "To (blank = home)" : "To"}><Input value={leg.to} onChange={(e) => setExtraLegs(extraLegs.map((l, j) => j === i ? { ...l, to: e.target.value.toUpperCase() } : l))} placeholder="e.g. YYZ" /></Field>
                  <Field label="Nights at destination"><Stepper value={leg.nights} onChange={(v) => setExtraLegs(extraLegs.map((l, j) => j === i ? { ...l, nights: v } : l))} suffix="nights" /></Field>
                </div>
              </div>
            ))}
            {extraLegs.length < 3 ? (
              <Btn variant="secondary" size="sm" icon="plus" onClick={() => setExtraLegs([...extraLegs.slice(0, -1), { from: "", to: extraLegs[extraLegs.length - 1]?.from ?? "", nights: "3" }, ...extraLegs.slice(-1)])}>Add a leg</Btn>
            ) : null}
          </FormSection>
        ) : (
          <FormSection label="Return leg">
            <div className="ds-row ds-gap-3 ds-start" style={{ background: "var(--surface-soft)", border: "1px solid var(--border)", borderRadius: "var(--r-sm)", padding: "12px 14px" }}>
              <span style={{ color: "var(--accent-600)", display: "flex", marginTop: 1 }}><Icon name="swap" size={16} /></span>
              <div style={{ fontSize: 13, color: "var(--text-soft)" }}>
                <span style={{ fontWeight: 600, color: "var(--ink)" }}>Auto-mirrored from outbound.</span> Return flies {to.join(", ") || "destination"} -&gt; {from.join(", ") || "origin"} after the stay.
              </div>
            </div>
          </FormSection>
        )}

        {/* Schedule */}
        <FormSection label="Schedule & market">
          <div className="ds-grid ds-g-3">
            <Field label="Travel window — from"><Input type="date" defaultValue="2026-07-01" /></Field>
            <Field label="Travel window — to"><Input type="date" defaultValue="2027-06-30" /></Field>
            <Field label="Booking window"><Stepper value={days} max={730} suffix="days" onChange={setDays} /></Field>
          </div>
          <div className="ds-grid ds-g-2">
            <Field label="Market">
              <select className="ds-input" value={market} onChange={(e) => setMarket(e.target.value)}>
                {MARKETS.map(([v, l]) => <option key={v} value={v}>{v.toUpperCase()} — {l}</option>)}
              </select>
            </Field>
            <Field label="Currency">
              <select className="ds-input" value={currency} onChange={(e) => setCurrency(e.target.value)}>
                {CURRENCIES.map((c) => <option key={c}>{c}</option>)}
              </select>
            </Field>
          </div>
        </FormSection>

        {/* Filters */}
        <FormSection label="Fare filters">
          <Field label="Stops" help="Max stops per leg.">
            <ChipRow options={STOPS} value={stops} onChange={setStops} />
          </Field>
          <Field label="Max layover" help="Caps the halt at each stop; longer is excluded.">
            <div className="ds-row ds-wrap ds-gap-2">
              <ChipRow options={LAYOVER} value={layover} onChange={setLayover} />
              <Input value={layover} onChange={(e) => setLayover(e.target.value.replace(/\D/g, ""))} placeholder="Custom hours" style={{ width: 130, height: 38 }} />
            </div>
          </Field>
          <Field label="Max leg duration" help="Filters each leg independently; legs are not summed.">
            <div className="ds-row ds-wrap ds-gap-2">
              <ChipRow options={LEGDUR} value={legdur} onChange={setLegdur} />
              <Input value={legdur} onChange={(e) => setLegdur(e.target.value.replace(/\D/g, ""))} placeholder="Custom hours" style={{ width: 130, height: 38 }} />
            </div>
          </Field>
          {/* Same-airline toggle — prototype hardcoded "always on"; live app has a real toggle */}
          <label className="ds-row ds-gap-3 ds-start" style={{ background: "var(--surface-soft)", border: "1px solid var(--border)", borderRadius: "var(--r-sm)", padding: "12px 14px", cursor: "pointer" }}>
            <input type="checkbox" checked={sameAirline} onChange={(e) => setSameAirline(e.target.checked)} style={{ marginTop: 2 }} />
            <span style={{ fontSize: 13, color: "var(--text-soft)" }}>
              <b style={{ color: "var(--ink)", fontWeight: 600 }}>Same airline only</b> — only itineraries flown by ONE airline on every leg qualify; the cheapest of those is saved.
            </span>
          </label>
        </FormSection>

        <div className="ds-row ds-between" style={{ paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <p style={{ fontSize: 12, color: "var(--muted)", maxWidth: 320 }}>Cheapest valid same-airline fare is saved per date.</p>
          <div className="ds-row ds-gap-2">
            <Btn variant="secondary">Cancel</Btn>
            <Btn variant="primary" icon="plus">Create group</Btn>
          </div>
        </div>
      </div>
    </div>
  );
}
