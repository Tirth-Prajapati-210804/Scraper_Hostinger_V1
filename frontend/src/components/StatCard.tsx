import { type LucideIcon } from "lucide-react";

import { cn } from "../utils/cn";

import { Card } from "./ui/Card";

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
    <Card className="group rounded-[12px] border-[#E8ECF4] bg-white px-5 py-[18px] shadow-none transition-all duration-150 hover:shadow-[0_4px_18px_rgba(75,94,222,0.08)]">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-medium uppercase tracking-[0.06em] text-[#9CA3AF]">
            {label}
          </p>
        </div>

        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] bg-[#F4F6FA] text-[#6B7280]">
          <Icon className="h-[15px] w-[15px]" />
        </div>
      </div>

      <p
        className={cn(
          "truncate text-[26px] font-bold leading-none text-[#1a1d23]",
          valueClassName,
        )}
      >
        {value}
      </p>

      {subtitle ? <p className="mt-2 truncate text-[12px] text-[#9CA3AF]">{subtitle}</p> : null}
    </Card>
  );
}
