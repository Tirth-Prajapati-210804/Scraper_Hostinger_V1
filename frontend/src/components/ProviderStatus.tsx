import {
  CheckCircle2,
  PlugZap,
} from "lucide-react";

import { type HealthResponse } from "../types/stats";
import { Card } from "./ui/Card";

interface ProviderStatusProps {
  health?: HealthResponse;
}

export function ProviderStatus({
  health,
}: ProviderStatusProps) {
  const providerStatus =
    health?.provider_status ?? {};

  const providers =
    Object.entries(providerStatus);

  if (providers.length === 0) {
    return (
      <Card className="rounded-[12px] border-[#E8ECF4] bg-white px-5 py-[14px] shadow-none">
        <div className="flex items-center gap-3">
          <div className="flex h-[34px] w-[34px] items-center justify-center rounded-[8px] bg-[#F4F6FA] text-[#6B7280]">
            <PlugZap className="h-4 w-4" />
          </div>

          <div>
            <p className="text-[13px] font-semibold text-[#1a1d23]">Provider Status</p>
            <p className="text-[11px] text-[#9CA3AF]">No provider data available</p>
          </div>
        </div>
      </Card>
    );
  }

  return (
    <Card className="rounded-[12px] border-[#E8ECF4] bg-white px-5 py-[14px] shadow-none">
      {providers.map(([name, status], index) => {
        const active = status === "configured" || status === "active";

        return (
          <div
            key={name}
            className={`flex items-center justify-between ${index > 0 ? "mt-3 border-t border-[#F4F6FA] pt-3" : ""}`}
          >
            <div className="flex items-center gap-[10px]">
              <div className="flex h-[34px] w-[34px] items-center justify-center rounded-[8px] bg-[#F4F6FA] text-[#6B7280]">
                {active ? (
                  <CheckCircle2 className="h-[15px] w-[15px] text-emerald-600" />
                ) : (
                  <PlugZap className="h-[15px] w-[15px] text-slate-400" />
                )}
              </div>
              <div>
                <div className="text-[13px] font-semibold text-[#1a1d23] capitalize">{name}</div>
                <div className="text-[11px] text-[#9CA3AF]">
                  {active ? "Operational" : "Unavailable"}
                </div>
              </div>
            </div>

            <span
              className={`inline-flex items-center gap-1 rounded-full px-2 py-[2px] text-[12px] font-medium ${
                active ? "bg-[#ECFDF5] text-[#059669]" : "bg-[#F1F5F9] text-[#64748B]"
              }`}
            >
              {active ? <span className="h-[5px] w-[5px] rounded-full bg-[#10B981]" /> : null}
              {active ? "Operational" : status}
            </span>
          </div>
        );
      })}
    </Card>
  );
}
