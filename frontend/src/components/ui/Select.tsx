import {
  Children,
  isValidElement,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../../utils/cn";

interface SelectChangeEvent {
  target: { value: string };
  currentTarget: { value: string };
}

interface SelectProps {
  label?: string;
  id?: string;
  "aria-label"?: string;
  value?: string;
  defaultValue?: string;
  disabled?: boolean;
  className?: string;
  children: ReactNode;
  onChange?: (event: SelectChangeEvent) => void;
}

interface SelectOption {
  value: string;
  label: string;
  disabled: boolean;
}

type OptionElement = ReactElement<{
  value?: string | number;
  disabled?: boolean;
  children?: ReactNode;
}>;

function optionLabel(children: ReactNode): string {
  return Children.toArray(children).join("");
}

function extractOptions(children: ReactNode): SelectOption[] {
  const options: SelectOption[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement(child)) return;
    const option = child as OptionElement;
    const label = optionLabel(option.props.children);
    const rawValue = option.props.value ?? label;
    options.push({
      value: String(rawValue),
      label,
      disabled: Boolean(option.props.disabled),
    });
  });
  return options;
}

export function Select({
  label,
  className,
  id,
  children,
  value,
  defaultValue,
  disabled,
  onChange,
  "aria-label": ariaLabel,
}: SelectProps) {
  const generatedId = useId();
  const buttonId = id ?? generatedId;
  const options = useMemo(() => extractOptions(children), [children]);
  const [open, setOpen] = useState(false);
  const [internalValue, setInternalValue] = useState(defaultValue ?? options[0]?.value ?? "");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selectedValue = value ?? internalValue;
  const selected = options.find((option) => option.value === selectedValue) ?? options[0];

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [open]);

  function selectValue(nextValue: string) {
    const next = options.find((option) => option.value === nextValue);
    if (!next || next.disabled) return;
    setInternalValue(next.value);
    onChange?.({
      target: { value: next.value },
      currentTarget: { value: next.value },
    });
    setOpen(false);
  }

  function moveSelection(direction: 1 | -1) {
    const enabled = options.filter((option) => !option.disabled);
    if (!enabled.length) return;
    const currentIndex = enabled.findIndex((option) => option.value === selectedValue);
    const nextIndex = currentIndex < 0 ? 0 : (currentIndex + direction + enabled.length) % enabled.length;
    selectValue(enabled[nextIndex].value);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      moveSelection(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      moveSelection(-1);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setOpen((current) => !current);
    }
  }

  return (
    <div ref={rootRef} className="relative space-y-1.5">
      {label && (
        <label
          htmlFor={buttonId}
          className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500"
        >
          {label}
        </label>
      )}

      <button
        id={buttonId}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={handleKeyDown}
        className={cn(
          "flex h-11 w-full items-center justify-between rounded-2xl border border-slate-200 bg-white px-4 text-left text-sm text-slate-700 shadow-sm transition",
          "focus:border-brand-500 focus:outline-none focus:ring-4 focus:ring-brand-500/10",
          "hover:border-slate-300 disabled:bg-slate-50 disabled:text-slate-400",
          className,
        )}
      >
        <span className="truncate">{selected?.label ?? "Select"}</span>
        <ChevronDown className="ml-3 h-4 w-4 shrink-0 text-slate-400" />
      </button>

      {open && !disabled ? (
        <div className="absolute left-0 right-0 z-50 mt-1 max-h-64 overflow-auto rounded-2xl border border-slate-200 bg-white p-1 shadow-[0_24px_70px_-35px_rgba(15,23,42,0.35)]">
          <div role="listbox" aria-labelledby={buttonId}>
            {options.map((option) => {
              const active = option.value === selectedValue;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={active}
                  disabled={option.disabled}
                  onClick={() => selectValue(option.value)}
                  className={cn(
                    "flex w-full items-center rounded-xl px-3 py-2 text-left text-sm transition",
                    active ? "bg-brand-50 text-brand-700" : "text-slate-700 hover:bg-slate-50",
                    "disabled:cursor-not-allowed disabled:text-slate-300",
                  )}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
