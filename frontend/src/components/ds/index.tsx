/**
 * Design-system UI kit (TS port of the .redesign prototype's ui.jsx).
 * Emits the semantic classes defined in src/design-system.css. Icons map to
 * lucide-react so we reuse the installed icon set. Import from "@/components/ds"
 * or a relative path.
 *
 * These coexist with the existing Tailwind components; pages adopt them stage by
 * stage. Nothing here changes app behavior - presentation primitives only.
 */
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
} from "react";
import {
  Activity,
  ArrowLeft,
  ArrowRightLeft,
  Calendar,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Database,
  Download,
  Globe,
  Layers,
  List,
  LogOut,
  LayoutGrid,
  Mail,
  MapPin,
  Minus,
  Pencil,
  Plane,
  Play,
  Plus,
  RefreshCw,
  Search,
  Shield,
  Square,
  Trash2,
  TriangleAlert,
  UserPlus,
  Users,
  X,
  type LucideIcon,
} from "lucide-react";

function cx(...a: Array<string | false | null | undefined>): string {
  return a.filter(Boolean).join(" ");
}

/* ----- Icon ------------------------------------------------------------- */
const ICONS: Record<string, LucideIcon> = {
  activity: Activity,
  back: ArrowLeft,
  swap: ArrowRightLeft,
  calendar: Calendar,
  check: Check,
  chevdown: ChevronDown,
  chevleft: ChevronLeft,
  chevright: ChevronRight,
  help: CircleHelp,
  database: Database,
  download: Download,
  globe: Globe,
  layers: Layers,
  list: List,
  logout: LogOut,
  grid: LayoutGrid,
  mail: Mail,
  pin: MapPin,
  minus: Minus,
  pencil: Pencil,
  plane: Plane,
  play: Play,
  plus: Plus,
  refresh: RefreshCw,
  search: Search,
  shield: Shield,
  square: Square,
  trash: Trash2,
  alert: TriangleAlert,
  userplus: UserPlus,
  users: Users,
  viewport: LayoutGrid,
  x: X,
};

export type IconName = keyof typeof ICONS;

export function Icon({ name, size = 16 }: { name: IconName | string; size?: number }) {
  const Cmp = ICONS[name] ?? CircleHelp;
  return <Cmp size={size} strokeWidth={2} />;
}

/* ----- Button ----------------------------------------------------------- */
type BtnVariant = "primary" | "secondary" | "ghost" | "danger";
export function Btn({
  variant = "primary",
  size,
  icon,
  iconRight,
  loading,
  children,
  className,
  ...rest
}: {
  variant?: BtnVariant;
  size?: "sm" | "lg" | "block";
  icon?: IconName | string;
  iconRight?: IconName | string;
  loading?: boolean;
  children?: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cx("btn", `btn--${variant}`, size && `btn--${size}`, className)}
      disabled={rest.disabled || loading}
      {...rest}
    >
      {loading ? <span className="spin" /> : icon ? <Icon name={icon} size={15} /> : null}
      {children}
      {iconRight ? <Icon name={iconRight} size={15} /> : null}
    </button>
  );
}

export function IconBtn({
  icon,
  title,
  spinning,
  className,
  ...rest
}: {
  icon: IconName | string;
  title?: string;
  spinning?: boolean;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button className={cx("iconbtn", className)} title={title} {...rest}>
      <span className={spinning ? "spin" : undefined} style={{ display: "flex" }}>
        <Icon name={icon} size={15} />
      </span>
    </button>
  );
}

/* ----- Card ------------------------------------------------------------- */
export function Card({
  hover,
  pad0,
  className,
  children,
  ...rest
}: {
  hover?: boolean;
  pad0?: boolean;
  className?: string;
  children?: ReactNode;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cx("card", hover && "card--hover", pad0 && "card--pad0", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

/* ----- Badge / chips ---------------------------------------------------- */
type Tone = "accent" | "neutral" | "success" | "warning" | "danger";
export function Badge({ tone = "neutral", dot, children }: { tone?: Tone; dot?: boolean; children: ReactNode }) {
  return (
    <span className={cx("badge", `badge--${tone}`)}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

type ChipTone = "ok" | "info" | "warn" | "danger";
export function StatusChip({ tone = "ok", dot, icon, children }: { tone?: ChipTone; dot?: boolean; icon?: IconName | string; children: ReactNode }) {
  return (
    <span className={cx("statuschip", `statuschip--${tone}`)}>
      {dot && <span className="dot" />}
      {icon && <Icon name={icon} size={13} />}
      {children}
    </span>
  );
}

export function CodeTag({ children }: { children: ReactNode }) {
  return <span className="codetag">{children}</span>;
}

/* ----- Stat / mini-stat ------------------------------------------------- */
export function StatCard({ label, value, sub, icon }: { label: string; value: ReactNode; sub?: ReactNode; icon: IconName | string }) {
  return (
    <Card hover className="stat">
      <div className="stat__top">
        <span className="stat__label">{label}</span>
        <span className="stat__icon"><Icon name={icon} /></span>
      </div>
      <div className="stat__value">{value}</div>
      {sub ? <div className="stat__sub">{sub}</div> : null}
    </Card>
  );
}

export function MiniStat({ k, v }: { k: ReactNode; v: ReactNode }) {
  return (
    <div className="ministat">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}

/* ----- Field / input ---------------------------------------------------- */
export function Field({ label, hint, help, children }: { label?: ReactNode; hint?: ReactNode; help?: string; children: ReactNode }) {
  return (
    <label className="field">
      {label ? (
        <span className="field__label">
          {label}
          {help && (
            <span title={help} style={{ color: "var(--muted)", display: "flex" }}>
              <Icon name="help" size={14} />
            </span>
          )}
        </span>
      ) : null}
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx("ds-input", className)} {...rest} />;
}

export function SearchInput({ value, onChange, placeholder, width }: { value: string; onChange: (e: React.ChangeEvent<HTMLInputElement>) => void; placeholder?: string; width?: number }) {
  return (
    <div className="input-search" style={width ? { width } : undefined}>
      <Icon name="search" />
      <input className="ds-input" value={value} onChange={onChange} placeholder={placeholder} />
    </div>
  );
}

/* ----- Segmented control ----------------------------------------------- */
export function Seg<T extends string>({
  options,
  value,
  onChange,
  icons,
}: {
  options: Array<{ id: T; label?: string; icon?: IconName | string }>;
  value: T;
  onChange: (id: T) => void;
  icons?: boolean;
}) {
  return (
    <div className={cx("seg", icons && "seg--icons")}>
      {options.map((o) => (
        <button key={o.id} className={value === o.id ? "is-active" : undefined} onClick={() => onChange(o.id)}>
          {o.icon ? <Icon name={o.icon} /> : o.label}
        </button>
      ))}
    </div>
  );
}

/* ----- Progress bar ----------------------------------------------------- */
export function Bar({ pct, warn }: { pct: number; warn?: boolean }) {
  return (
    <div className={cx("bar", warn && "bar--warn")}>
      <i style={{ width: Math.min(pct, 100) + "%" }} />
    </div>
  );
}

/* ----- Page / section headers ------------------------------------------ */
export function PageHeader({ title, subtitle, eyebrow, children }: { title: ReactNode; subtitle?: ReactNode; eyebrow?: ReactNode; children?: ReactNode }) {
  return (
    <div className="ds-row ds-between ds-wrap ds-gap-4" style={{ marginBottom: 24 }}>
      <div>
        {eyebrow ? <div className="eyebrow" style={{ marginBottom: 4 }}>{eyebrow}</div> : null}
        <h1 className="page-title">{title}</h1>
        {subtitle ? <p className="page-sub">{subtitle}</p> : null}
      </div>
      {children ? <div className="ds-row ds-wrap ds-gap-2">{children}</div> : null}
    </div>
  );
}

export function SectionHead({ title, sub, children }: { title: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="sechead">
      <div>
        <h2>{title}</h2>
        {sub ? <p>{sub}</p> : null}
      </div>
      {children ? <div className="ds-row ds-wrap ds-gap-2">{children}</div> : null}
    </div>
  );
}

/* ----- Empty / banner --------------------------------------------------- */
export function Empty({ icon, title, text }: { icon: IconName | string; title: ReactNode; text?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty__icon"><Icon name={icon} /></div>
      <h3>{title}</h3>
      {text ? <p>{text}</p> : null}
    </div>
  );
}

export function Banner({ tone = "warn", title, children, icon = "alert" }: { tone?: "warn" | "danger"; title: ReactNode; children?: ReactNode; icon?: IconName | string }) {
  return (
    <div className={cx("banner", `banner--${tone}`)}>
      <Icon name={icon} />
      <div><b>{title}</b>{children}</div>
    </div>
  );
}

export { cx };
