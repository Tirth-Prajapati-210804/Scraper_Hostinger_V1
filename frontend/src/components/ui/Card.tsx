import { type ComponentPropsWithoutRef, type ReactNode } from "react";
import { cn } from "../../utils/cn";

interface CardProps extends ComponentPropsWithoutRef<"div"> {
  children: ReactNode;
}

export function Card({
  children,
  className,
  ...props
}: CardProps) {
  return (
    <div
      {...props}
      className={cn(
        "max-w-full min-w-0 rounded-[12px] border border-[#E8ECF4] bg-white p-5 shadow-none transition-all duration-150",
        className
      )}
    >
      {children}
    </div>
  );
}
