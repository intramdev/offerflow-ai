// Date rendering honoring the user's display preference ("us" or "eu").

export type DateTimeFormat = "us" | "eu";

function parts(iso: string): { y: string; m: string; d: string; time: string } | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    y: String(date.getFullYear()),
    m: pad(date.getMonth() + 1),
    d: pad(date.getDate()),
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  };
}

export function formatDate(iso: string, format: DateTimeFormat): string {
  const p = parts(iso);
  if (!p) return iso;
  return format === "eu" ? `${p.d}/${p.m}/${p.y}` : `${p.m}/${p.d}/${p.y}`;
}

export function formatDateTime(iso: string, format: DateTimeFormat): string {
  const p = parts(iso);
  if (!p) return iso;
  const day = format === "eu" ? `${p.d}/${p.m}/${p.y}` : `${p.m}/${p.d}/${p.y}`;
  return `${day} ${p.time}`;
}

export function formatMoney(value: string | number): string {
  const num = typeof value === "number" ? value : Number(String(value).trim());
  if (!Number.isFinite(num)) return String(value);
  return num.toFixed(2);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
