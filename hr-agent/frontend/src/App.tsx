import { useCallback, useEffect, useReducer, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import Markdown from "react-markdown";
import { chat, createSession, fetchMe, getDemoPersona, setDemoPersona, type Me } from "./api";
import { cancelMessage, confirmMessage, initialState, reducer } from "./agui/reducer";
import type { Citation, Turn } from "./agui/types";
import { ConfirmationCard } from "./components/ConfirmationCard";
import { CitationChip, CitationDrawer } from "./components/Citations";
import { RefusalCard, TracePanel } from "./components/Trace";
import { WidgetView } from "./components/widgets/Widgets";
import { Decor, Squiggle } from "./components/ui/Decor";
import { IconBadge, type Tone } from "./components/ui/IconBadge";
import {
  ArrowRight, CalendarDays, Monitor, Plane, RotateCcw, ShieldCheck, Sparkles, Ticket, Wallet, type LucideIcon,
} from "lucide-react";

const SUGGESTIONS: { text: string; icon: LucideIcon; tone: Tone }[] = [
  { text: "How many vacation days do I get?", icon: CalendarDays, tone: "accent" },
  { text: "What's my current leave balance?", icon: Wallet, tone: "secondary" },
  { text: "Book vacation leave on 2 and 3 November", icon: Plane, tone: "tertiary" },
  { text: "I work from home — can I get a new monitor?", icon: Monitor, tone: "quaternary" },
  { text: "Show my open service-desk tickets", icon: Ticket, tone: "accent" },
];

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [me, setMe] = useState<Me | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [citation, setCitation] = useState<Citation | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const threadRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback(async () => {
    abortRef.current?.abort();
    dispatch({ kind: "reset" });
    setBootError(null);
    setSessionId(null);
    try {
      const [profile, session] = await Promise.all([fetchMe(), createSession()]);
      setMe(profile);
      setSessionId(session.session_id);
    } catch (e) {
      setBootError((e as Error).message || "The HR assistant is unavailable right now.");
    }
  }, []);

  useEffect(() => {
    void start();
  }, [start]);

  // Keep the newest content in view while streaming, unless the user scrolled up to read.
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
  }, [state.turns]);

  const last = state.turns.at(-1);
  // One polite announcement per completed reply (not per token — avoids screen-reader spam).
  useEffect(() => {
    if (last?.role === "assistant" && !last.streaming) {
      setAnnouncement(last.error ? "The assistant could not complete the request." : "The assistant replied.");
    }
  }, [last?.streaming, last?.role, last?.error]);

  const send = useCallback(async (text: string) => {
    const message = text.trim();
    if (!message || !sessionId || state.running) return;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    dispatch({ kind: "user", id: crypto.randomUUID(), text: message });
    try {
      for await (const ev of chat(sessionId, message, ctrl.signal)) dispatch({ kind: "event", ev });
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      dispatch({ kind: "network_error", message: (e as Error).message || "Connection lost. Nothing was submitted." });
    }
  }, [sessionId, state.running]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const text = draft;
    setDraft("");
    void send(text);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      onSubmit(e);
    }
  };

  const decide = (pid: string, decision: "confirm" | "cancel") => {
    dispatch({ kind: "decide", proposalId: pid, decision });
    void send(decision === "confirm" ? confirmMessage(pid) : cancelMessage(pid));
  };

  const switchPersona = (id: string) => {
    setDemoPersona(id);
    void start();
  };

  const prefill = (text: string) => {
    setDraft(text);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(text.length, text.length);
    });
  };

  const lastAssistant = [...state.turns].reverse().find((t) => t.role === "assistant");

  return (
    <div className="app">
      <Decor />
      <header className="topbar">
        <div className="brand">
          <span className="brand__logo" aria-hidden="true">HR</span>
          <div>
            <h1>Altostrat HR Assistant</h1>
            <p className="brand__sub">Policy answers with citations · leave · service desk</p>
          </div>
        </div>
        <div className="topbar__right">
          {me?.personas ? (
            <label className="persona">
              <span className="persona__label">Demo persona</span>
              <select value={getDemoPersona() || me.employee_id} onChange={(e) => switchPersona(e.target.value)}>
                {me.personas.map((p) => (
                  <option key={p.employee_id} value={p.employee_id}>
                    {p.name} · {p.location_status} ({p.employee_id})
                  </option>
                ))}
              </select>
            </label>
          ) : (
            me && <span className="persona__badge">{me.name ?? me.employee_id}</span>
          )}
          <button type="button" className="btn" onClick={() => void start()}>
            <RotateCcw className="btn__glyph" strokeWidth={2.5} aria-hidden="true" />
            New chat
          </button>
        </div>
      </header>

      <main className="layout">
        <section className="chat" aria-label="Conversation">
          <div className="thread" ref={threadRef}>
            {bootError && (
              <div className="notice notice--error" role="alert">
                {bootError} <button type="button" className="btn btn--link" onClick={() => void start()}>Retry</button>
              </div>
            )}
            {state.turns.length === 0 && !bootError && (
              <div className="welcome">
                <h2 className="welcome__title">
                  Hi{me?.name ? <> <span className="welcome__name">{me.name.split(" ")[0]}</span></> : ""}, how can I help?
                  <Squiggle className="welcome__squiggle" />
                </h2>
                <p className="muted">
                  I answer from the Altostrat Singapore Employee Policy Handbook and can help with your leave and
                  service-desk tickets. I never submit anything without your confirmation.
                </p>
                <ul className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <li key={s.text}>
                      <button type="button" className="suggestion" disabled={!sessionId} onClick={() => void send(s.text)}>
                        <IconBadge icon={s.icon} tone={s.tone} size="sm" />
                        {s.text}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {state.turns.map((t) => (
              <TurnView key={t.id} turn={t} state={state} onCite={setCitation} onDecide={decide}
                onAsk={(text) => void send(text)} onPrefill={prefill} />
            ))}
          </div>

          <form className="composer" onSubmit={onSubmit}>
            <label htmlFor="composer-input" className="visually-hidden">Message the HR assistant</label>
            <textarea
              id="composer-input"
              ref={inputRef}
              rows={1}
              value={draft}
              placeholder={sessionId ? "Ask about policy, your leave, or a ticket…" : "Connecting…"}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
              maxLength={4000}
              enterKeyHint="send"
            />
            <button type="submit" className="btn btn--primary" disabled={!sessionId || state.running || !draft.trim()}>
              {state.running ? "Working…" : "Send"}
              <span className="btn__icon" aria-hidden="true"><ArrowRight strokeWidth={2.5} /></span>
            </button>
          </form>
          <p className="disclaimer">
            <ShieldCheck className="disclaimer__icon" strokeWidth={2.5} aria-hidden="true" />
            Answers are generated from the governed handbook; check the cited section. Don't share NRIC numbers or
            other sensitive identifiers.
          </p>
        </section>

        <TracePanel turn={lastAssistant} running={state.running} />
      </main>

      <CitationDrawer citation={citation} onClose={() => setCitation(null)} />
      <div className="visually-hidden" aria-live="polite" aria-atomic="true">{announcement}</div>
    </div>
  );
}

type TurnProps = {
  turn: Turn;
  state: ReturnType<typeof reducer>;
  onCite: (c: Citation) => void;
  onDecide: (pid: string, d: "confirm" | "cancel") => void;
  onAsk: (text: string) => void;
  onPrefill: (text: string) => void;
};

function TurnView({ turn, state, onCite, onDecide, onAsk, onPrefill }: TurnProps) {
  if (turn.role === "user") {
    return (
      <div className="msg msg--user">
        <div className="bubble">{turn.text}</div>
      </div>
    );
  }
  const waiting = turn.streaming && !turn.text && !turn.refusal && !turn.error;
  const runningStep = [...turn.trace].reverse().find((s) => s.status === "running");
  return (
    <div className="msg msg--assistant">
      <IconBadge icon={Sparkles} tone="secondary" className="avatar" />
      <div className="msg__body">
        {turn.refusal && <RefusalCard block={turn.refusal} />}
        {turn.widgets.map((w) => (
          <WidgetView key={w.id} widget={w} onAsk={onAsk} onPrefill={onPrefill} disabled={state.running} />
        ))}
        {waiting && (
          <p className="typing">
            <span className="dots" aria-hidden="true"><i /><i /><i /></span>
            {runningStep ? runningStep.label + "…" : "Thinking…"}
          </p>
        )}
        {turn.text && (
          <div className={`bubble bubble--md${turn.streaming ? " bubble--streaming" : ""}`}>
            <Markdown>{turn.text}</Markdown>
          </div>
        )}
        {turn.proposalIds.map((pid) => state.proposals[pid] && (
          <ConfirmationCard key={pid} proposal={state.proposals[pid]} disabled={state.running}
            onDecide={(d) => onDecide(pid, d)} />
        ))}
        {turn.citations.length > 0 && (
          <div className="citations">
            <span className="citations__label">Sources</span>
            {turn.citations.map((c) => <CitationChip key={c.anchor} citation={c} onOpen={onCite} />)}
          </div>
        )}
        {turn.error && (
          <div className="notice notice--error" role="alert">{turn.error}</div>
        )}
      </div>
    </div>
  );
}
