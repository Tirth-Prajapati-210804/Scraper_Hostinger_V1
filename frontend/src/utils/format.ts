function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function isValidDate(date: Date): boolean {
  return !Number.isNaN(date.getTime());
}

export function formatDisplayDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  const dateOnly = dateStr.slice(0, 10);
  const [year, month, day] = dateOnly.split("-").map(Number);
  if (!year || !month || !day) return dateStr;
  return `${pad(day)}-${pad(month)}-${year}`;
}

export function formatDisplayDateTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "Never";
  const date = new Date(dateStr);
  if (!isValidDate(date)) return "Never";

  let hour = date.getHours();
  const minute = date.getMinutes();
  const suffix = hour >= 12 ? "PM" : "AM";
  hour %= 12;
  if (hour === 0) hour = 12;

  return `${formatDisplayDate(
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
  )} ${pad(hour)}:${pad(minute)} ${suffix}`;
}

export function formatRelativeTime(dateStr: string | null | undefined): string {
  return formatDisplayDateTime(dateStr);
}

export function formatFreshnessLabel(dateStr: string | null | undefined): string {
  if (!dateStr) return "Not scraped yet";
  return `Scraped ${formatDisplayDateTime(dateStr)}`;
}

export function formatNumber(n: number): string {
  return n.toLocaleString();
}

export function formatPercent(n: number): string {
  return `${n.toFixed(1)}%`;
}
