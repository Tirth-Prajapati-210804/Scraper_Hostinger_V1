import {
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";

import { cn } from "../../utils/cn";

interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?:
  | "primary"
  | "secondary"
  | "ghost"
  | "danger";

  size?: "sm" | "md" | "lg";

  children: ReactNode;
  loading?: boolean;
}

export function Button({
  variant = "primary",
  size = "md",
  children,
  loading = false,
  disabled,
  className,
  ...props
}: ButtonProps) {
  const base = `
    inline-flex items-center justify-center gap-2
    whitespace-nowrap rounded-[8px]
    font-medium
    transition-all duration-150
    focus:outline-none focus:ring-2 focus:ring-brand-500/25
    disabled:pointer-events-none disabled:opacity-55
    select-none
  `;

  const sizes = {
    sm: "px-3 py-1.5 text-[13px]",
    md: "px-4 py-2 text-[13px]",
    lg: "px-5 py-[11px] text-[13px]",
  };

  const variants = {
    primary: `
      bg-[#4B5EDE] text-white
      hover:bg-[#4354cd]
      active:bg-[#3d4dc1]
    `,

    secondary: `
      border border-[#E2E8F0] bg-white text-[#374151]
      hover:bg-slate-50
      active:bg-slate-100
    `,

    ghost: `
      bg-transparent text-[#374151]
      hover:bg-slate-100 hover:text-slate-900
      active:bg-slate-200
    `,

    danger: `
      border border-[#FCA5A5] bg-[#FEF2F2] text-[#DC2626]
      hover:bg-[#fee2e2]
      active:bg-[#fecaca]
    `,
  };

  return (
    <button
      className={cn(
        base,
        sizes[size],
        variants[variant],
        className
      )}
      disabled={disabled || loading}
      aria-disabled={
        disabled || loading
      }
      {...props}
    >
      {loading && (
        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
      )}

      {children}
    </button>
  );
}
