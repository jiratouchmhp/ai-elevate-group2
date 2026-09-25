import { Ban, Check, CircleCheck, CircleX, ClipboardCheck, LoaderCircle, TriangleAlert, X, type LucideIcon } from "lucide-react";
import type { Proposal } from "../agui/types";
import { IconBadge, type Tone } from "./ui/IconBadge";
import { LeaveCalendar } from "./widgets/LeaveCalendar";

const LABELS: Record<string, string> = {
  start_date: "Start date",
  end_date: "End date",
  leave_type: "Leave type",
  days: "Working days",
  half_day: "Half day",
  request_id: "Leave request",
  address: "New address",
  phone: "New phone",
  category: "Category",
  short_description: "Summary",
  description: "Details",
  priority: "Priority",
  purpose: "Purpose",
  estimated_cost_usd: "Estimated cost (USD)",
  ticket_id: "Ticket",
  comment: "Comment",
  new_status: "New status",
  resolution_notes: "Resolution notes",
  balance_after: "Balance after",
  remaining_after: "Balance after",
  ship_to: "Ship to",
};

const HIDDEN = new Set(["user_confirmed_resolved"]);

const human = (k: string) => LABELS[k] ?? k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "object") return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${human(k)}: ${fmt(x)}`).join(", ");
  return String(v);
}

const STATUS_COPY: Record<Proposal["status"], string> = {
  awaiting_confirmation: "Awaiting your confirmation",
  confirming: "Submitting…",
  cancelled: "Cancelled — nothing was submitted",
  committed: "Submitted",
  denied: "Not submitted",
  failed: "Could not be completed — nothing was left half-done",
  expired: "No longer pending",
};

const STATUS_ICON: Record<Proposal["status"], { icon: LucideIcon; tone: Tone }> = {
  awaiting_confirmation: { icon: ClipboardCheck, tone: "accent" },
  confirming: { icon: LoaderCircle, tone: "accent" },
  committed: { icon: CircleCheck, tone: "quaternary" },
  denied: { icon: CircleX, tone: "bad" },
  failed: { icon: CircleX, tone: "bad" },
  cancelled: { icon: Ban, tone: "muted" },
  expired: { icon: Ban, tone: "muted" },
};

type Props = {
  proposal: Proposal;
  disabled: boolean;
  onDecide: (decision: "confirm" | "cancel") => void;
};

/**
 * B-3 as a UI invariant (SDD §3.10): the parsed intent is rendered as structured fields
 * with explicit Confirm / Cancel, never inferred from free text like "yeah ok".
 */
export function ConfirmationCard({ proposal: p, disabled, onDecide }: Props) {
  const rows = [
    ...Object.entries(p.proposed).filter(([k]) => !HIDDEN.has(k)),
    ...Object.entries(p.computed).filter(([k]) => !(k in p.proposed) && !HIDDEN.has(k)),
  ];
  const titleId = `card-${p.proposal_id}`;
  const pending = p.status === "awaiting_confirmation";
  const head = STATUS_ICON[p.status];
  return (
    <section className={`card card--${p.status}`} aria-labelledby={titleId}>
      <IconBadge icon={head.icon} tone={head.tone} size="lg" className="card__float" />
      {pending && <span className="card__star" aria-hidden="true">Needs you</span>}
      <header className="card__head">
        <h3 id={titleId}>{p.title}</h3>
        <span className={`pill pill--${p.status}`}>{STATUS_COPY[p.status]}</span>
      </header>
      {p.action === "submit_leave" && <LeaveCalendar start={p.proposed.start_date} end={p.proposed.end_date} />}
      <dl className="card__fields">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{human(k)}</dt>
            <dd>{fmt(v)}</dd>
          </div>
        ))}
      </dl>
      {p.warnings.length > 0 && (
        <div className="card__callout card__callout--warn">
          <IconBadge icon={TriangleAlert} tone="tertiary" size="sm" />
          <ul className="card__warnings">
            {p.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}
      {p.status === "committed" && p.backend_ref && (
        <p className="card__receipt">Reference <strong>{p.backend_ref}</strong></p>
      )}
      {(p.status === "denied" || p.status === "failed") && p.reasons && p.reasons.length > 0 && (
        <div className="card__callout card__callout--bad">
          <IconBadge icon={CircleX} tone="bad" size="sm" />
          <ul className="card__reasons">{p.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}
      {pending && (
        <div className="card__actions">
          <button type="button" className="btn btn--primary" disabled={disabled} onClick={() => onDecide("confirm")}>
            Confirm<span className="visually-hidden"> {p.title}</span>
            <span className="btn__icon" aria-hidden="true"><Check strokeWidth={2.5} /></span>
          </button>
          <button type="button" className="btn" disabled={disabled} onClick={() => onDecide("cancel")}>
            <X className="btn__glyph" strokeWidth={2.5} aria-hidden="true" />
            Cancel<span className="visually-hidden"> {p.title}</span>
          </button>
        </div>
      )}
      <p className="card__foot">Nothing is submitted until you confirm. Proposal {p.proposal_id}</p>
    </section>
  );
}
