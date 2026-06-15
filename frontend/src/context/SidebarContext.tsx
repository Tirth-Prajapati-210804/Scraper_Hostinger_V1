import { createContext, useContext, useState, type ReactNode } from "react";

interface SidebarState {
  /** Desktop: narrow (icon-only) vs full-width sidebar. */
  collapsed: boolean;
  toggle: () => void;
  /** Mobile: whether the slide-in nav drawer is open. */
  mobileOpen: boolean;
  openMobile: () => void;
  closeMobile: () => void;
}

const SidebarContext = createContext<SidebarState | null>(null);

const STORAGE_KEY = "fh.sidebar.collapsed";

export function SidebarProvider({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [mobileOpen, setMobileOpen] = useState(false);

  const toggle = () =>
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });

  const openMobile = () => setMobileOpen(true);
  const closeMobile = () => setMobileOpen(false);

  return (
    <SidebarContext.Provider value={{ collapsed, toggle, mobileOpen, openMobile, closeMobile }}>
      {children}
    </SidebarContext.Provider>
  );
}

export function useSidebar(): SidebarState {
  const ctx = useContext(SidebarContext);
  if (!ctx) {
    return {
      collapsed: false,
      toggle: () => {},
      mobileOpen: false,
      openMobile: () => {},
      closeMobile: () => {},
    };
  }
  return ctx;
}
