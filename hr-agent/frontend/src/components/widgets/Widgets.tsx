import type { ReactNode } from "react";
import { CalendarDays, LifeBuoy, Ticket, UserRound, Wallet, type LucideIcon } from "lucide-react";
import { IconBadge, type Tone } from "../ui/IconBadge";
import type { LeaveBalance, LeaveRequest, Profile, TicketComment, TicketLite, Widget } from "../../agui/types";
import { fmtDay, fmtRange, initials, num, slug, tenure } from "./format";

export type WidgetActions = {
  /** Send a follow-up message on the user's behalf (e.g. open a ticket). */
  onAsk: (text: string) => void;
  /** Put text in the composer for the user to finish (e.g. dates for a leave request). */
  onPrefill: (text: string) => void;
  disabled: boolean;
};

/**
 * Generative UI: renders structured tool results as rich cards next to the assistant's prose.
 * Data comes from the BFF's allow-listed widget payloads (app/ui_events.py) — never raw tool output.
 */
export function WidgetView({ widget, ...actions }: { widget: Widget } & WidgetActions) {
  switch (widget.kind) {
    case "leave_balance":
      return <LeaveBalanceWidget balances={widget.data.balances} {...actions} />;
    case "leave_requests":
      return <LeaveRequestsWidget requests={widget.data.requests} />;
    case "ticket_list":
      return <TicketListWidget tickets={widget.data.tickets} {...actions} />;
    case "ticket_detail":
      return <TicketDetailWidget ticket={widget.data} />;
    case "profile":
      return <ProfileWidget profile={widget.data} />;
  }
}

type ShellProps = { title: string; source: string; icon: LucideIcon; tone: Tone; children: ReactNode };

function Shell({ title, source, icon, tone, children }: ShellProps) {
  return (
    <article className={`widget widget--${tone}`}>
      <header className="widget__head">
        <IconBadge icon={icon} tone={tone} size="sm" />
        <h3>{title}</h3>
        <span className="widget__source">Live from {source}</span>
      </header>
      {children}
    </article>
  );
}

// ------------------------------------------------------------------ leave balance
function Ring({ b }: { b: LeaveBalance }) {
  const total = b.accrued ?? (b.remaining ?? 0) + (b.used ?? 0) + (b.pending ?? 0);
  const pct = total > 0 ? Math.max(0, Math.min(1, (b.remaining ?? 0) / total)) : 0;
  const tone = pct <= 0.15 ? "low" : pct <= 0.35 ? "mid" : "ok";
  return (
    <figure className={`ring ring--${tone}`}>
      <svg viewBox="0 0 42 42" className="ring__svg" aria-hidden="true">
        <circle className="ring__track" cx="21" cy="21" r="15.915" />
        <circle className="ring__value" cx="21" cy="21" r="15.915" pathLength="100"
          strokeDasharray={`${(pct * 100).toFixed(1)} 100`} />
      </svg>
      <div className="ring__center" aria-hidden="true">
        <strong>{num(b.remaining)}</strong>
        <span>left</span>
      </div>
      <figcaption>
        <span className="ring__type">{b.type}</span>
        <span className="ring__detail">
          {num(b.remaining)} of {num(total)} days left
          {b.used !== null ? ` · ${num(b.used)} used` : ""}
          {b.pending ? ` · ${num(b.pending)} pending` : ""}
        </span>
      </figcaption>
    </figure>
  );
}

function LeaveBalanceWidget({ balances, onPrefill, disabled }: { balances: LeaveBalance[] } & WidgetActions) {
  return (
    <Shell title="Your leave balance" source="WorkWeek" icon={Wallet} tone="accent">
      <div className="rings">{balances.map((b) => <Ring key={b.type} b={b} />)}</div>
      <div className="widget__actions">
        <button type="button" className="btn btn--sm" disabled={disabled}
          onClick={() => onPrefill("Book vacation leave from ")}>
          Book leave
        </button>
      </div>
    </Shell>
  );
}

// ------------------------------------------------------------------ leave requests
function LeaveRequestsWidget({ requests }: { requests: LeaveRequest[] }) {
  return (
    <Shell title="Your leave requests" source="WorkWeek" icon={CalendarDays} tone="secondary">
      {requests.length === 0 ? (
        <p className="muted widget__empty">No leave requests on record.</p>
      ) : (
        <ul className="rows">
          {requests.map((r, i) => (
            <li key={r.request_id ?? i} className="row">
              <span className={`dot dot--${slug(r.leave_type)}`} aria-hidden="true" />
              <div className="row__main">
                <strong>{fmtRange(r.start_date, r.end_date)}</strong>
                <span className="muted">
                  {r.leave_type ?? "Leave"}{r.days !== undefined ? ` · ${num(r.days)} day${r.days === 1 ? "" : "s"}` : ""}
                  {r.request_id ? ` · ${r.request_id}` : ""}
                </span>
              </div>
              {r.status && <span className={`status status--${slug(r.status)}`}>{r.status}</span>}
            </li>
          ))}
        </ul>
      )}
    </Shell>
  );
}

// ------------------------------------------------------------------ tickets
function Priority({ p }: { p?: string }) {
  if (!p) return null;
  return <span className={`prio prio--${slug(p)}`}>{p.replace(/^\d+\s*-\s*/, "")}</span>;
}

function TicketListWidget({ tickets, onAsk, disabled }: { tickets: TicketLite[] } & WidgetActions) {
  return (
    <Shell title="Your service-desk tickets" source="ServiceImmediately" icon={Ticket} tone="tertiary">
      {tickets.length === 0 ? (
        <p className="muted widget__empty">You have no tickets.</p>
      ) : (
        <ul className="rows">
          {tickets.map((t, i) => (
            <li key={t.ticket_id ?? i} className="row">
              <div className="row__main">
                <strong>{t.short_description ?? "Untitled ticket"}</strong>
                <span className="muted">
                  {t.ticket_id}{t.category ? ` · ${t.category}` : ""}{t.created_at ? ` · opened ${fmtDay(t.created_at)}` : ""}
                </span>
              </div>
              <Priority p={t.priority} />
              {t.status && <span className={`status status--${slug(t.status)}`}>{t.status}</span>}
              {t.ticket_id && (
                <button type="button" className="btn btn--sm" disabled={disabled}
                  onClick={() => onAsk(`Show me the details of ticket ${t.ticket_id}`)}>
                  View<span className="visually-hidden"> {t.ticket_id}</span>
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Shell>
  );
}

const LIFECYCLE = ["New", "In Progress", "Resolved", "Closed"];

function Stepper({ status }: { status?: string }) {
  const idx = LIFECYCLE.findIndex((s) => s.toLowerCase() === (status ?? "").toLowerCase());
  return (
    <ol className="stepper" aria-label={`Ticket status: ${status ?? "unknown"}`}>
      {LIFECYCLE.map((s, i) => (
        <li key={s} className={`stepper__step${i < idx ? " is-done" : ""}${i === idx ? " is-current" : ""}`}
          aria-current={i === idx ? "step" : undefined}>
          <span className="stepper__dot" aria-hidden="true" />
          <span className="stepper__label">{s}</span>
        </li>
      ))}
      {idx < 0 && status && <li className="stepper__other"><span className="status">{status}</span></li>}
    </ol>
  );
}

const WHO: Record<TicketComment["by"], string> = { you: "You", support: "Support", assistant: "HR Assistant" };

function TicketDetailWidget({ ticket: t }: { ticket: TicketLite & { description?: string | null; comments: TicketComment[] } }) {
  return (
    <Shell title={`${t.ticket_id ?? "Ticket"} · ${t.short_description ?? ""}`} source="ServiceImmediately" icon={LifeBuoy} tone="quaternary">
      <Stepper status={t.status} />
      <dl className="meta">
        {t.category && <div><dt>Category</dt><dd>{t.category}</dd></div>}
        {t.priority && <div><dt>Priority</dt><dd><Priority p={t.priority} /></dd></div>}
        {t.assignee && <div><dt>Assigned to</dt><dd>{t.assignee}</dd></div>}
        {t.created_at && <div><dt>Opened</dt><dd>{fmtDay(t.created_at)}</dd></div>}
      </dl>
      {t.description && <p className="widget__desc">{t.description}</p>}
      {t.comments.length > 0 && (
        <ol className="timeline" aria-label="Comment history">
          {t.comments.map((c, i) => (
            <li key={i} className={`timeline__item timeline__item--${c.by}`}>
              <span className="timeline__who">{WHO[c.by]}{c.at ? ` · ${fmtDay(c.at)}` : ""}</span>
              <p>{c.text}</p>
            </li>
          ))}
        </ol>
      )}
    </Shell>
  );
}

// ------------------------------------------------------------------ profile
function ProfileWidget({ profile: p }: { profile: Profile }) {
  const t = tenure(p.hire_date);
  return (
    <Shell title="Your WorkWeek profile" source="WorkWeek" icon={UserRound} tone="accent">
      <div className="profile">
        <span className="profile__avatar" aria-hidden="true">{initials(p.name)}</span>
        <div className="profile__main">
          <strong className="profile__name">{p.name ?? "—"}</strong>
          <span className="muted">{[p.role, p.department].filter(Boolean).join(" · ")}</span>
          <div className="badges">
            {p.location_status && <span className={`badge badge--${slug(p.location_status)}`}>{p.location_status}</span>}
            {p.office && <span className="badge">{p.office} office</span>}
            {t && <span className="badge" title={p.hire_date ? `Joined ${fmtDay(p.hire_date)}` : undefined}>{t} at Altostrat</span>}
            {p.manager_name && <span className="badge">Manager: {p.manager_name}</span>}
          </div>
        </div>
      </div>
    </Shell>
  );
}
