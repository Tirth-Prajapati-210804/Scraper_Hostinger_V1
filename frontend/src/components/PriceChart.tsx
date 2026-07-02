import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PriceTrend } from "../types/price";
import { formatDisplayDate } from "../utils/format";

interface PriceChartProps {
  data: PriceTrend[];
  /** Group currency (e.g. GBP, CAD); axis + tooltip follow it instead of a
   *  hardcoded dollar sign. */
  currency?: string;
}

// "£1,234" / "CA$1,234" etc. from the group's currency code; falls back to a
// plain number with the code suffix if Intl rejects the code.
function fmtMoney(value: unknown, currency?: string): string {
  const num = Number(value) || 0;
  const code = (currency ?? "").trim().toUpperCase();
  if (!code) return `${num.toLocaleString()}`;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
      maximumFractionDigits: 0,
    }).format(num);
  } catch {
    return `${num.toLocaleString()} ${code}`;
  }
}

function fmtDate(d: unknown): string {
  if (typeof d !== "string") return String(d ?? "");
  return formatDisplayDate(d);
}

interface CustomTooltipProps {
  active?: boolean;
  label?: string;
  payload?: Array<{ payload: PriceTrend }>;
  currency?: string;
}

function CustomTooltip({ active, payload, label, currency }: CustomTooltipProps) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload as PriceTrend;
  return (
    <div
      className="rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-md"
      style={{ fontSize: 13 }}
    >
      <p className="mb-1 font-medium text-slate-700">{fmtDate(label)}</p>
      <p className="text-brand-600 font-semibold">
        {fmtMoney(point.price, currency)}
      </p>
      {point.airline && (
        <p className="mt-0.5 text-xs text-slate-400">{point.airline}</p>
      )}
    </div>
  );
}

export function PriceChart({ data, currency }: PriceChartProps) {
  if (!data.length) {
    return (
      <p className="py-12 text-center text-sm text-slate-400">
        No price data available
      </p>
    );
  }

  return (
    <div className="w-full max-w-full overflow-x-hidden">
      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={data} margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
          <defs>
            <linearGradient id="priceGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#3C5681" stopOpacity={0.22} />
              <stop offset="95%" stopColor="#3C5681" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 12, fill: "#64748b" }}
            tickFormatter={fmtDate}
            minTickGap={40}
          />
          <YAxis
            tick={{ fontSize: 12, fill: "#64748b" }}
            tickFormatter={(v: unknown) => fmtMoney(v, currency)}
            width={68}
          />
          <Tooltip content={<CustomTooltip currency={currency} />} />
          <Area
            type="monotone"
            dataKey="price"
            stroke="#3C5681"
            strokeWidth={2}
            fill="url(#priceGradient)"
            dot={false}
            activeDot={{ r: 4, strokeWidth: 0 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
