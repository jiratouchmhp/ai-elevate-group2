import type { AguiEvent, ChatState, Proposal, Turn, Widget } from "./types";

export type Action =
  | { kind: "user"; id: string; text: string }
  | { kind: "event"; ev: AguiEvent }
  | { kind: "decide"; proposalId: string; decision: "confirm" | "cancel" }
  | { kind: "network_error"; message: string }
  | { kind: "reset" };

export const initialState: ChatState = { turns: [], proposals: {}, running: false };

const newTurn = (id: string, role: Turn["role"], text = ""): Turn => ({
  id, role, text, streaming: role === "assistant", citations: [], trace: [], proposalIds: [], transactions: [], widgets: [],
});

/** One widget per kind per turn (per ticket for details): a re-read replaces the earlier card. */
const widgetKey = (w: Widget) => (w.kind === "ticket_detail" ? `${w.kind}:${w.data.ticket_id ?? w.id}` : w.kind);

/** Apply `fn` to the last assistant turn (the one the current run streams into). */
function withCurrent(state: ChatState, fn: (t: Turn) => Turn): ChatState {
  const i = state.turns.length - 1;
  if (i < 0 || state.turns[i].role !== "assistant") return state;
  const turns = state.turns.slice();
  turns[i] = fn(turns[i]);
  return { ...state, turns };
}

const COMMIT_TERMINAL: Record<string, Proposal["status"]> = {
  committed: "committed", already_committed: "committed", denied: "denied", failed: "failed",
};

function applyEvent(state: ChatState, ev: AguiEvent): ChatState {
  switch (ev.type) {
    case "RUN_STARTED":
      return { ...state, running: true };
    case "RUN_FINISHED":
      return withCurrent({ ...state, running: false }, (t) => ({ ...t, streaming: false }));
    case "RUN_ERROR":
      return withCurrent({ ...state, running: false }, (t) => ({ ...t, streaming: false, error: ev.message }));
    case "TEXT_MESSAGE_START":
      // several text messages in one run are rendered as paragraphs of the same bubble
      return withCurrent(state, (t) => {
        const text = t.text && !t.text.endsWith("\n\n") ? t.text + "\n\n" : t.text;
        return { ...t, text, msgStart: text.length };
      });
    case "TEXT_MESSAGE_CONTENT":
      return withCurrent(state, (t) => ({ ...t, text: t.text + ev.delta }));
    case "TEXT_MESSAGE_END":
      return state;
    case "TOOL_CALL_START":
      return withCurrent(state, (t) => ({
        ...t,
        trace: [...t.trace, {
          id: ev.toolCallId, name: ev.toolCallName, label: ev.label, agent: ev.agent, status: "running",
          ...(ev.system ? { system: ev.system } : {}),
        }],
      }));
    case "TOOL_CALL_END":
      return withCurrent(state, (t) => ({
        ...t,
        trace: t.trace.map((s) => (s.id === ev.toolCallId ? { ...s, status: ev.status, latencyMs: ev.latencyMs } : s)),
      }));
    case "STATE_DELTA": {
      let next = state;
      for (const op of ev.delta) {
        const pid = op.path.replace(/^\/pending_proposals\//, "");
        if (!pid || pid === op.path) continue;
        if (op.op === "add" || op.op === "replace") {
          if (!op.value) continue;
          next = { ...next, proposals: { ...next.proposals, [pid]: op.value } };
          next = withCurrent(next, (t) => (t.proposalIds.includes(pid) ? t : { ...t, proposalIds: [...t.proposalIds, pid] }));
        } else if (op.op === "remove") {
          const p = next.proposals[pid];
          if (p && (p.status === "awaiting_confirmation" || p.status === "confirming")) {
            next = { ...next, proposals: { ...next.proposals, [pid]: { ...p, status: "expired" } } };
          }
        }
      }
      return next;
    }
    case "CUSTOM": {
      if (ev.name === "citation") {
        return withCurrent(state, (t) =>
          t.citations.some((c) => c.anchor === ev.value.anchor) ? t : { ...t, citations: [...t.citations, ev.value] });
      }
      if (ev.name === "message_replace") {
        // The output guardrail screened the final text: it supersedes the streamed deltas.
        return withCurrent(state, (t) => ({ ...t, text: t.text.slice(0, t.msgStart ?? 0) + ev.value.text }));
      }
      if (ev.name === "guardrail_block") {
        return withCurrent(state, (t) => ({ ...t, refusal: ev.value }));
      }
      if (ev.name === "widget") {
        const w = ev.value;
        return withCurrent(state, (t) => {
          const i = t.widgets.findIndex((x) => widgetKey(x) === widgetKey(w));
          if (i < 0) return { ...t, widgets: [...t.widgets, w] };
          const widgets = t.widgets.slice();
          widgets[i] = w;
          return { ...t, widgets };
        });
      }
      if (ev.name === "transaction") {
        const tx = ev.value;
        let next = withCurrent(state, (t) => ({ ...t, transactions: [...t.transactions, tx] }));
        const p = tx.proposal_id ? next.proposals[tx.proposal_id] : undefined;
        const b3Pending = tx.status === "denied" && tx.rule_ids.includes("B3_CONFIRMATION_REQUIRED");
        if (p && tx.stage === "commit" && !b3Pending && COMMIT_TERMINAL[tx.status]) {
          next = {
            ...next,
            proposals: {
              ...next.proposals,
              [p.proposal_id]: { ...p, status: COMMIT_TERMINAL[tx.status], backend_ref: tx.backend_ref, reasons: tx.reasons },
            },
          };
        }
        return next;
      }
      return state;
    }
    default:
      return state;
  }
}

export function reducer(state: ChatState, action: Action): ChatState {
  switch (action.kind) {
    case "reset":
      return initialState;
    case "user":
      return {
        ...state,
        running: true,
        turns: [...state.turns, newTurn(`${action.id}-u`, "user", action.text), newTurn(`${action.id}-a`, "assistant")],
      };
    case "event":
      return applyEvent(state, action.ev);
    case "decide": {
      const p = state.proposals[action.proposalId];
      if (!p || p.status !== "awaiting_confirmation") return state;
      const status = action.decision === "confirm" ? "confirming" : "cancelled";
      return { ...state, proposals: { ...state.proposals, [p.proposal_id]: { ...p, status } } };
    }
    case "network_error":
      return withCurrent({ ...state, running: false }, (t) => ({ ...t, streaming: false, error: action.message }));
  }
}

/** Structured confirmation messages (B-3 stays enforced server-side: this is a NEW user turn). */
export const confirmMessage = (pid: string) => `Yes, I confirm proposal ${pid}.`;
export const cancelMessage = (pid: string) => `No, please cancel proposal ${pid}. Do not submit it.`;
