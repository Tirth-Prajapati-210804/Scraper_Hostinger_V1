import {
  History,
  LayoutDashboard,
  LogOut,
  Menu,
  Plane,
  Users,
  X,
} from "lucide-react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../../context/AuthContext";
import { useSidebar } from "../../context/SidebarContext";
import { cn } from "../../utils/cn";

const BASE_NAV = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/logs", icon: History, label: "Collection Logs" },
];

function initials(name?: string): string {
  if (!name) return "SA";
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "SA";
}

export function Sidebar() {
  const { user, logout } = useAuth();
  const { collapsed, toggle, mobileOpen, closeMobile } = useSidebar();

  const navItems = [
    ...BASE_NAV,
    ...(user?.role === "admin"
      ? [{ to: "/users", icon: Users, label: "User Management" }]
      : []),
  ];

  return (
    <>
      {/* Slide-out overlay drawer. Used on mobile (mobileOpen) AND on desktop when
          the sidebar is collapsed (the menu button reopens it) -- one polished
          full-width drawer instead of a narrow icon rail. */}
      <DrawerSidebar
        open={mobileOpen}
        onClose={closeMobile}
        navItems={navItems}
        user={user}
        logout={logout}
      />

    {/* Desktop docked sidebar -- shown only when NOT collapsed. Collapsing hides it
        entirely (lg:hidden) and the header menu button opens the drawer above. */}
    <aside
      className={cn(
        "fixed inset-y-0 left-0 z-30 hidden w-[220px] shrink-0 flex-col border-r border-[#E8ECF4] bg-white",
        collapsed ? "lg:hidden" : "lg:flex",
      )}
    >
      <div className="flex items-center gap-[10px] border-b border-[#E8ECF4] px-4 pb-4 pt-5">
        <button
          onClick={toggle}
          aria-label="Collapse sidebar"
          title="Collapse sidebar"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] text-[#6B7280] transition hover:bg-[#F8FAFF]"
        >
          <Menu className="h-5 w-5" />
        </button>
        <div className="flex min-w-0 items-center gap-[10px]">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] bg-brand-600">
            <Plane className="h-[15px] w-[15px] text-white" />
          </div>
          <p className="truncate text-[13px] font-bold leading-[1.2] text-[#1a1d23]">
            Flight Scraper
          </p>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3">
        <p className="px-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-[#C4CAD4]">
          Navigation
        </p>

        <nav aria-label="Main navigation" className="mt-1.5 space-y-0.5">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "group flex items-center gap-[10px] rounded-[8px] px-[10px] py-[9px] text-[13px] transition-all",
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
                  <span className="truncate">{label}</span>
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </div>

      <div className="border-t border-[#E8ECF4] px-4 py-3">
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
      </div>
    </aside>
    </>
  );
}

type NavItem = { to: string; icon: typeof LayoutDashboard; label: string };

function DrawerSidebar({
  open,
  onClose,
  navItems,
  user,
  logout,
}: {
  open: boolean;
  onClose: () => void;
  navItems: NavItem[];
  user: ReturnType<typeof useAuth>["user"];
  logout: () => void;
}) {
  return (
    <div className={open ? "" : "pointer-events-none"} aria-hidden={!open}>
      {/* Backdrop */}
      <div
        onClick={onClose}
        className={cn(
          "fixed inset-0 z-40 bg-black/40 transition-opacity duration-200",
          open ? "opacity-100" : "opacity-0",
        )}
      />
      {/* Drawer */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-[260px] max-w-[80%] flex-col border-r border-[#E8ECF4] bg-white transition-transform duration-200",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center justify-between border-b border-[#E8ECF4] px-5 pb-4 pt-5">
          <div className="flex min-w-0 items-center gap-[10px]">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] bg-brand-600">
              <Plane className="h-[15px] w-[15px] text-white" />
            </div>
            <p className="truncate text-[13px] font-bold leading-[1.2] text-[#1a1d23]">
              Flight Scraper
            </p>
          </div>
          <button
            onClick={onClose}
            title="Close menu"
            className="flex h-8 w-8 items-center justify-center rounded-[7px] text-[#9CA3AF] transition hover:bg-[#F8FAFF] hover:text-[#6B7280]"
          >
            <X className="h-[18px] w-[18px]" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-3 py-3">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-[#C4CAD4]">
            Navigation
          </p>
          <nav aria-label="Main navigation" className="mt-1.5 space-y-0.5">
            {navItems.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                onClick={onClose}
                className={({ isActive }) =>
                  cn(
                    "group flex items-center gap-[10px] rounded-[8px] px-[10px] py-[11px] text-[14px] transition-all",
                    isActive
                      ? "bg-[#EEF2FF] font-semibold text-brand-700"
                      : "font-normal text-[#6B7280] hover:bg-[#F8FAFF]",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon className={cn("h-[16px] w-[16px] shrink-0", isActive ? "text-brand-700" : "text-[#9CA3AF]")} />
                    <span className="truncate">{label}</span>
                  </>
                )}
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="border-t border-[#E8ECF4] px-4 py-3">
          <div className="mb-[10px] flex items-center gap-[10px]">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#EEF2FF] text-[13px] font-bold text-brand-700">
              {initials(user?.full_name)}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[12px] font-semibold text-[#1a1d23]">{user?.full_name}</div>
              <div className="truncate text-[11px] text-[#9CA3AF]">{user?.email}</div>
            </div>
          </div>
          <button
            onClick={logout}
            className="flex w-full items-center gap-[6px] rounded-[7px] border border-[#E8ECF4] bg-white px-[10px] py-[9px] text-[13px] text-[#6B7280] transition hover:bg-slate-50"
          >
            <LogOut className="h-[14px] w-[14px]" />
            Sign Out
          </button>
        </div>
      </aside>
    </div>
  );
}
