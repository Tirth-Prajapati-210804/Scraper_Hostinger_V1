import { type ReactNode } from "react";
import { Menu } from "lucide-react";
import { useLocation, useMatch } from "react-router-dom";
import { useSidebar } from "../../context/SidebarContext";
import { Sidebar } from "./Sidebar";

function usePageTitle(): string {
  const location = useLocation();
  const isDetail = useMatch("/route-groups/:id");
  if (isDetail) return "Route Group Detail";
  const titles: Record<string, string> = {
    "/": "Dashboard",
    "/logs": "Collection Logs",
    "/users": "User Management",
  };
  return titles[location.pathname] ?? "Flight Price Tracker";
}

interface AppLayoutProps {
  children: ReactNode;
}

export function AppLayout({ children }: AppLayoutProps) {
  const pageTitle = usePageTitle();
  const { collapsed, openMobile } = useSidebar();

  return (
    <div className="flex min-h-screen overflow-x-hidden bg-transparent">
      <Sidebar />
      <div
        className={`flex min-h-screen min-w-0 flex-1 flex-col transition-[padding] duration-200 ${
          collapsed ? "lg:pl-0" : "lg:pl-[220px]"
        }`}
      >
        {/* Top bar with the menu button. Always shown on mobile; on desktop it
            appears only when the sidebar is collapsed, so the drawer can be
            reopened. openMobile drives the shared slide-out drawer in Sidebar. */}
        <header
          className={`sticky top-0 z-20 flex items-center gap-3 border-b border-[#E8ECF4] bg-white/95 px-4 py-3 backdrop-blur ${
            collapsed ? "" : "lg:hidden"
          }`}
        >
          <button
            onClick={openMobile}
            aria-label="Open menu"
            className="flex h-9 w-9 items-center justify-center rounded-[8px] border border-[#E8ECF4] text-[#6B7280] transition hover:bg-[#F8FAFF]"
          >
            <Menu className="h-5 w-5" />
          </button>
          <span className="truncate text-[14px] font-semibold text-[#1a1d23]">{pageTitle}</span>
        </header>

        <main className="min-w-0 flex-1 overflow-x-hidden px-4 pb-6 pt-6 sm:px-6 lg:overflow-y-auto lg:px-9 lg:pb-8 lg:pt-8">
          {children}
        </main>
      </div>
    </div>
  );
}
