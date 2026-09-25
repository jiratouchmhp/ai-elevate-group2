// Small, dependency-free formatting helpers shared by the generative-UI widgets.

/** Parse "YYYY-MM-DD" (or an ISO datetime) as a LOCAL calendar date (no UTC day-shift). */
export function parseDate(s?: string | null): Date | null {
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

const DAY = new Intl.DateTimeFormat("en-SG", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const SHORT = new Intl.DateTimeFormat("en-SG", { day: "numeric", month: "short" });

/** "Tue 22 Dec 2026" — matches the orchestrator's date style. */
export const fmtDay = (s?: string | null) => {
  const d = parseDate(s);
  return d ? DAY.format(d) : s ?? "—";
};

export function fmtRange(start?: string, end?: string): string {
  const a = parseDate(start);
  const b = parseDate(end);
  if (!a) return "—";
  if (!b || a.getTime() === b.getTime()) return DAY.format(a);
  return a.getFullYear() === b.getFullYear() ? `${SHORT.format(a)} – ${DAY.format(b)}` : `${DAY.format(a)} – ${DAY.format(b)}`;
}

export function tenure(hire?: string, today = new Date()): string | null {
  const h = parseDate(hire);
  if (!h) return null;
  let months = (today.getFullYear() - h.getFullYear()) * 12 + (today.getMonth() - h.getMonth());
  if (today.getDate() < h.getDate()) months -= 1;
  if (months < 0) return null;
  const y = Math.floor(months / 12);
  const m = months % 12;
  if (y === 0) return `${m} mo`;
  return m ? `${y} yr ${m} mo` : `${y} yr`;
}

export const num = (n: number | null | undefined) =>
  n === null || n === undefined ? "—" : Number.isInteger(n) ? String(n) : n.toFixed(1);

export const initials = (name?: string) =>
  (name ?? "?").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]!.toUpperCase()).join("");

/** CSS-safe modifier from free-form status/priority text ("In Progress" -> "in-progress"). */
export const slug = (s?: string) => (s ?? "unknown").toLowerCase().replace(/^\d+\s*-\s*/, "").replace(/[^a-z0-9]+/g, "-");
