import {
  History,
  LayoutDashboard,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Plane,
  Table,
  Users,
} from "lucide-react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../../context/AuthContext";
import { useSidebar } from "../../context/SidebarContext";
import { cn } from "../../utils/cn";

const BASE_NAV = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/explorer", icon: Table, label: "Data Explorer" },
  { to: "/logs", icon: History, label: "Collection Logs" },
];

function initials(name?: string): string {
  if (!name) return "SA";
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "SA";
}

export function Sidebar() {
  const { user, logout } = useAuth();
  const { collapsed, toggle } = useSidebar();

  const navItems = [
    ...BASE_NAV,
    ...(user?.role === "admin"
      ? [{ to: "/users", icon: Users, label: "User Management" }]
      : []),
  ];

  return (
    <aside
      className={cn(
        "fixed inset-y-0 left-0 z-30 hidden shrink-0 border-r border-[#E8ECF4] bg-white transition-[width] duration-200 lg:flex lg:flex-col",
        collapsed ? "w-[68px]" : "w-[220px]",
      )}
    >
      <div className={cn("flex items-center border-b border-[#E8ECF4] pb-4 pt-5", collapsed ? "justify-center px-2" : "justify-between px-5")}>
        <div className="flex min-w-0 items-center gap-[10px]">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] bg-brand-600">
            <Plane className="h-[15px] w-[15px] text-white" />
          </div>
          {!collapsed ? (
            <p className="truncate text-[13px] font-bold leading-[1.2] text-[#1a1d23]">
              Flight Scraper
            </p>
          ) : null}
        </div>
        {!collapsed ? (
          <button
            onClick={toggle}
            title="Collapse sidebar"
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[#9CA3AF] transition hover:bg-[#F8FAFF] hover:text-[#6B7280]"
          >
            <PanelLeftClose className="h-[15px] w-[15px]" />
          </button>
        ) : null}
      </div>

      {collapsed ? (
        <div className="flex justify-center border-b border-[#E8ECF4] py-2">
          <button
            onClick={toggle}
            title="Expand sidebar"
            className="flex h-7 w-7 items-center justify-center rounded-[7px] text-[#9CA3AF] transition hover:bg-[#F8FAFF] hover:text-[#6B7280]"
          >
            <PanelLeftOpen className="h-[15px] w-[15px]" />
          </button>
        </div>
      ) : null}

      <div className={cn("flex-1 overflow-y-auto py-3", collapsed ? "px-2" : "px-3")}>
        {!collapsed ? (
          <p className="px-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-[#C4CAD4]">
            Navigation
          </p>
        ) : null}

        <nav aria-label="Main navigation" className="mt-1.5 space-y-0.5">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              title={collapsed ? label : undefined}
              className={({ isActive }) =>
                cn(
                  "group flex items-center rounded-[8px] text-[13px] transition-all",
                  collapsed ? "justify-center px-0 py-[10px]" : "gap-[10px] px-[10px] py-[9px]",
                  isActive
                    ? "bg-[#EEF2FF] font-semibold text-brand-700"
                    : "font-normal text-[#6B7280] hover:bg-[#F8FAFF] hover:text-[#6B7280]",
                )
              }
            >
              {({ isActive }) => (
                <>
                  <div
                    className={cn(
                      "flex h-[15px] w-[15px] shrink-0 items-center justify-center transition",
                      isActive ? "text-brand-700" : "text-[#9CA3AF]",
                    )}
                  >
                    <Icon className="h-[15px] w-[15px]" />
                  </div>
                  {!collapsed ? <span className="truncate">{label}</span> : null}
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </div>

      <div className={cn("border-t border-[#E8ECF4] py-3", collapsed ? "px-2" : "px-4")}>
        {collapsed ? (
          <div className="flex flex-col items-center gap-2">
            <div
              title={`${user?.full_name ?? ""} · ${user?.email ?? ""}`}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-[#EEF2FF] text-[12px] font-bold text-brand-700"
            >
              {initials(user?.full_name)}
            </div>
            <button
              onClick={logout}
              title="Sign Out"
              className="flex h-8 w-8 items-center justify-center rounded-[7px] border border-[#E8ECF4] text-[#6B7280] transition hover:bg-slate-50"
            >
              <LogOut className="h-[13px] w-[13px]" />
            </button>
          </div>
        ) : (
          <>
            <div className="mb-[10px] flex items-center gap-[10px]">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#EEF2FF] text-[13px] font-bold text-brand-700">
                {initials(user?.full_name)}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12px] font-semibold text-[#1a1d23]">
                  {user?.full_name}
                </div>
                <div className="truncate text-[11px] text-[#9CA3AF]">{user?.email}</div>
              </div>
            </div>
            <button
              onClick={logout}
              className="flex w-full items-center gap-[6px] rounded-[7px] border border-[#E8ECF4] bg-white px-[10px] py-[7px] text-[12px] text-[#6B7280] transition hover:bg-slate-50"
            >
              <LogOut className="h-[13px] w-[13px]" />
              Sign Out
            </button>
          </>
        )}
      </div>
    </aside>
  );
}
