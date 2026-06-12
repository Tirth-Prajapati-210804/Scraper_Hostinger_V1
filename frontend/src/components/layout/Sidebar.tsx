import {
  History,
  LayoutDashboard,
  LogOut,
  Plane,
  Table,
  Users,
} from "lucide-react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../../context/AuthContext";
import { cn } from "../../utils/cn";

const BASE_NAV = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/explorer", icon: Table, label: "Data Explorer" },
  { to: "/logs", icon: History, label: "Collection Logs" },
];

export function Sidebar() {
  const { user, logout } = useAuth();

  const navItems = [
    ...BASE_NAV,
    ...(user?.role === "admin"
      ? [
        {
          to: "/users",
          icon: Users,
          label: "User Management",
        },
      ]
      : []),
  ];

  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-[220px] shrink-0 border-r border-[#E8ECF4] bg-white lg:flex lg:flex-col">
      <div className="border-b border-[#E8ECF4] px-5 pb-4 pt-5">
        <div className="flex items-center gap-[10px]">
          <div className="flex h-8 w-8 items-center justify-center rounded-[8px] bg-brand-600">
            <Plane className="h-[15px] w-[15px] text-white" />
          </div>

          <div className="min-w-0">
            <p className="truncate text-[13px] font-bold leading-[1.2] text-[#1a1d23]">
              Flight Scraper
            </p>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3">
        <p className="px-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-[#C4CAD4]">
          Navigation
        </p>

        <nav aria-label="Main navigation" className="mt-1.5 space-y-0.5">
          {navItems.map(
            ({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) =>
                  cn(
                    "group flex items-center gap-[10px] rounded-[8px] px-[10px] py-[9px] text-[13px] transition-all",
                    isActive
                      ? "bg-[#EEF2FF] font-semibold text-brand-700"
                      : "font-normal text-[#6B7280] hover:bg-[#F8FAFF] hover:text-[#6B7280]"
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <div
                      className={cn(
                        "flex h-[15px] w-[15px] shrink-0 items-center justify-center transition",
                        isActive
                          ? "text-brand-700"
                          : "text-[#9CA3AF]"
                      )}
                    >
                      <Icon className="h-[15px] w-[15px]" />
                    </div>

                    <span className="truncate">
                      {label}
                    </span>
                  </>
                )}
              </NavLink>
            )
          )}
        </nav>
      </div>

      <div className="border-t border-[#E8ECF4] px-4 py-3">
        <div className="mb-[10px] flex items-center gap-[10px]">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[#EEF2FF] text-[13px] font-bold text-brand-700">
            SA
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12px] font-semibold text-[#1a1d23]">
              {user?.full_name}
            </div>
            <div className="truncate text-[11px] text-[#9CA3AF]">
              {user?.email}
            </div>
          </div>
        </div>

        <button
          onClick={logout}
          className="flex w-full items-center gap-[6px] rounded-[7px] border border-[#E8ECF4] bg-white px-[10px] py-[7px] text-[12px] text-[#6B7280] transition hover:bg-slate-50"
        >
          <LogOut className="h-[13px] w-[13px]" />
          Sign Out
        </button>
      </div>
    </aside>
  );
}
