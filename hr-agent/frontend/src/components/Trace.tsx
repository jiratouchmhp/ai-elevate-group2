import { CircleSlash, Check, HeartHandshake, LoaderCircle, Pause, ShieldAlert, ShieldX, TriangleAlert, type LucideIcon } from "lucide-react";
import type { GuardrailBlock, Turn } from "../agui/types";
import { AgentFlow } from "./AgentFlow";
import { IconBadge, type Tone } from "./ui/IconBadge";

const ICON: Record<string, { icon: LucideIcon; tone: Tone }> = {
  running: { icon: LoaderCircle, tone: "accent" },
  success: { icon: Check, tone: "quaternary" },
  done: { icon: Check, tone: "quaternary" },
  committed: { icon: Check, tone: "quaternary" },
  awaiting_confirmation: { icon: Pause, tone: "tertiary" },
  denied: { icon: ShieldX, tone: "bad" },
  no_match: { icon: CircleSlash, tone: "muted" },
  not_found: { icon: CircleSlash, tone: "muted" },
  unavailable: { icon: TriangleAlert, tone: "bad" },
  failed: { icon: TriangleAlert, tone: "bad" },
};
const FALLBACK = { icon: Check, tone: "muted" as Tone };

export function TracePanel({ turn, running }: { turn?: Turn; running: boolean }) {
  const steps = turn?.trace ?? [];
  return (
    <aside className="trace" aria-label="What the assistant is doing">
      <h2 className="trace__title">Agent flow</h2>
      <AgentFlow steps={steps} running={running && !!turn?.streaming} started={!!turn}
        refusal={turn?.refusal} error={!!turn?.error} />
      <h2 className="trace__title trace__title--sub">Steps</h2>
      {steps.length === 0 ? (
        <p className="muted">{running ? "Thinking…" : "Steps the assistant takes will appear here."}</p>
      ) : (
        <ol className="trace__list">
          {steps.map((s) => {
            const ic = ICON[s.status] ?? FALLBACK;
            return (
              <li key={s.id} className={`trace__step trace__step--${s.status}`}>
                <IconBadge icon={ic.icon} tone={ic.tone} size="sm" spin={s.status === "running"} className="trace__icon" />
                <span className="trace__label">{s.label}</span>
                <span className="trace__meta">
                  {s.status === "running" ? "in progress" : s.status.replace(/_/g, " ")}
                  {s.latencyMs !== undefined && s.status !== "running" ? ` · ${(s.latencyMs / 1000).toFixed(1)}s` : ""}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </aside>
  );
}

/** Refusals are a first-class, successful outcome — not an error (SDD §3.3/§3.10). */
export function RefusalCard({ block }: { block: GuardrailBlock }) {
  if (block.wellbeing) {
    return (
      <div className="notice notice--care" role="note">
        <IconBadge icon={HeartHandshake} tone="accent" size="sm" className="notice__icon" />
        <div>
          <strong>You're not alone.</strong> Support is available right now — Samaritans of Singapore (24h): 1767 ·
          Emergency: 995 · or the Employee Assistance Programme.
        </div>
      </div>
    );
  }
  return (
    <div className="notice notice--refusal" role="note">
      <IconBadge icon={ShieldAlert} tone="tertiary" size="sm" className="notice__icon" />
      <div>
        <strong>Outside what I can do.</strong> This request was stopped by a safety check, so nothing was looked up
        or submitted. For anything else, open an <em>HRSD</em> case in ServiceImmediately.
      </div>
    </div>
  );
}
