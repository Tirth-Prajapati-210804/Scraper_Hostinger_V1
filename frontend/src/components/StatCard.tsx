import { type LucideIcon } from "lucide-react";

import { cn } from "../utils/cn";

interface StatCardProps {
  label: string;
  value: string | number;
  icon: LucideIcon;
  valueClassName?: string;
  subtitle?: string;
}

export function StatCard({
  label,
  value,
  icon: Icon,
  valueClassName,
  subtitle,
}: StatCardProps) {
  return (
    <div className="card card--hover stat">
      <div className="stat__top">
        <span className="stat__label">{label}</span>
        <span className="stat__icon">
          <Icon size={16} />
        </span>
      </div>

      <div className={cn("stat__value truncate", valueClassName)}>{value}</div>

      {subtitle ? <div className="stat__sub truncate">{subtitle}</div> : null}
    </div>
  );
}
