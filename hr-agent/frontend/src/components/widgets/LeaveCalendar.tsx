import { parseDate } from "./format";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH = new Intl.DateTimeFormat("en-SG", { month: "long", year: "numeric" });
const MAX_MONTHS = 2;

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

/** Weeks (Mon-first) of the month containing `first`, as Date | null cells. */
function monthGrid(first: Date): (Date | null)[][] {
  const y = first.getFullYear();
  const m = first.getMonth();
  const lead = (new Date(y, m, 1).getDay() + 6) % 7; // Mon=0
  const days = new Date(y, m + 1, 0).getDate();
  const cells: (Date | null)[] = [...Array(lead).fill(null), ...Array.from({ length: days }, (_, i) => new Date(y, m, i + 1))];
  while (cells.length % 7) cells.push(null);
  return Array.from({ length: cells.length / 7 }, (_, w) => cells.slice(w * 7, w * 7 + 7));
}

/**
 * Visual preview of a proposed leave request inside its confirmation card: the requested
 * range, with weekends shown as not counted. Informational only — the PDP-computed working
 * days on the card remain the source of truth.
 */
export function LeaveCalendar({ start, end }: { start?: unknown; end?: unknown }) {
  const a = parseDate(typeof start === "string" ? start : null);
  const b = parseDate(typeof end === "string" ? end : null) ?? a;
  if (!a || !b || b < a) return null;
  const months: Date[] = [];
  for (let d = new Date(a.getFullYear(), a.getMonth(), 1); d <= b && months.length < MAX_MONTHS; d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) {
    months.push(d);
  }
  const today = new Date();
  return (
    <figure className="cal">
      <figcaption className="visually-hidden">Requested leave dates</figcaption>
      {months.map((m) => (
        <table key={m.toISOString()} className="cal__month">
          <caption>{MONTH.format(m)}</caption>
          <thead>
            <tr>{WEEKDAYS.map((w) => <th key={w} scope="col"><abbr title={w}>{w[0]}</abbr></th>)}</tr>
          </thead>
          <tbody>
            {monthGrid(m).map((week, wi) => (
              <tr key={wi}>
                {week.map((d, di) => {
                  if (!d) return <td key={di} />;
                  const inRange = d >= a && d <= b;
                  const weekend = di >= 5;
                  const cls = [
                    "cal__day",
                    inRange && !weekend && "is-leave",
                    inRange && weekend && "is-skipped",
                    weekend && "is-weekend",
                    sameDay(d, a) && "is-start",
                    sameDay(d, b) && "is-end",
                    sameDay(d, today) && "is-today",
                  ].filter(Boolean).join(" ");
                  return (
                    <td key={di} className={cls}>
                      <span>{d.getDate()}</span>
                      {inRange && <span className="visually-hidden">{weekend ? " (weekend, not counted)" : " (leave)"}</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      ))}
      <p className="cal__legend">
        <span className="cal__key cal__key--leave" aria-hidden="true" /> Leave day
        <span className="cal__key cal__key--skipped" aria-hidden="true" /> Weekend (not counted)
      </p>
    </figure>
  );
}
