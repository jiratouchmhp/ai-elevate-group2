// AG-UI event subset emitted by the BFF (app/ui_events.py) + HR-specific CUSTOM payloads.

export type Citation = {
  anchor: string;
  label: string;
  section?: string;
  semantic_topic?: string;
  score?: number;
};

export type Proposal = {
  proposal_id: string;
  action: string;
  title: string;
  agent: string;
  proposed: Record<string, unknown>;
  computed: Record<string, unknown>;
  warnings: string[];
  status: "awaiting_confirmation" | "confirming" | "cancelled" | "committed" | "denied" | "failed" | "expired";
  backend_ref?: string | null;
  reasons?: string[];
};

export type Transaction = {
  proposal_id: string | null;
  stage: "propose" | "commit";
  status: string;
  action?: string | null;
  backend_ref?: string | null;
  rule_ids: string[];
  reasons: string[];
  message?: string | null;
};

export type GuardrailBlock = { stage?: string; categories: string[]; wellbeing: boolean };

// ---- generative-UI widgets (app/ui_events.py WIDGETS; SPII-free by construction) ----
export type LeaveBalance = { type: string; accrued: number | null; used: number | null; pending: number | null; remaining: number | null };
export type LeaveRequest = { request_id?: string; leave_type?: string; start_date?: string; end_date?: string; days?: number; status?: string };
export type TicketLite = {
  ticket_id?: string; short_description?: string; category?: string; priority?: string; status?: string;
  created_at?: string; assignee?: string;
};
export type TicketComment = { by: "you" | "support" | "assistant"; text: string | null; at?: string | null };
export type Profile = {
  name?: string; department?: string; role?: string; hire_date?: string; location_status?: string; office?: string;
  manager_name?: string;
};

export type Widget = { id: string; agent: string } & (
  | { kind: "leave_balance"; data: { balances: LeaveBalance[] } }
  | { kind: "leave_requests"; data: { requests: LeaveRequest[] } }
  | { kind: "ticket_list"; data: { tickets: TicketLite[] } }
  | { kind: "ticket_detail"; data: TicketLite & { description?: string | null; comments: TicketComment[] } }
  | { kind: "profile"; data: Profile }
);

/** Node of the Agent Flow graph a tool call touches (see TOOL_SYSTEM in app/ui_events.py). */
export type FlowSystem = "policy_agent" | "workweek_agent" | "itsm_agent" | "handbook" | "workweek" | "itsm";

type Base = { timestamp?: number };
export type AguiEvent = Base &
  (
    | { type: "RUN_STARTED"; threadId: string; runId: string }
    | { type: "RUN_FINISHED"; threadId: string; runId: string }
    | { type: "RUN_ERROR"; message: string; code?: string }
    | { type: "TEXT_MESSAGE_START"; messageId: string; role: "assistant" }
    | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
    | { type: "TEXT_MESSAGE_END"; messageId: string }
    | { type: "TOOL_CALL_START"; toolCallId: string; toolCallName: string; label: string; agent: string; system?: FlowSystem | null }
    | { type: "TOOL_CALL_END"; toolCallId: string; toolCallName: string; agent: string; status: string; latencyMs: number; system?: FlowSystem | null }
    | { type: "STATE_DELTA"; delta: { op: "add" | "remove" | "replace"; path: string; value?: Proposal }[] }
    | { type: "CUSTOM"; name: "citation"; value: Citation }
    | { type: "CUSTOM"; name: "transaction"; value: Transaction }
    | { type: "CUSTOM"; name: "guardrail_block"; value: GuardrailBlock }
    | { type: "CUSTOM"; name: "widget"; value: Widget }
    | { type: "CUSTOM"; name: "message_replace"; value: { messageId: string; text: string } }
  );

export type TraceStep = {
  id: string;
  name: string;
  label: string;
  agent: string;
  status: string; // running | success | awaiting_confirmation | denied | ...
  latencyMs?: number;
  system?: FlowSystem | null;
};

export type Turn = {
  id: string;
  role: "user" | "assistant";
  text: string;
  streaming: boolean;
  citations: Citation[];
  trace: TraceStep[];
  proposalIds: string[];
  transactions: Transaction[];
  widgets: Widget[];
  refusal?: GuardrailBlock;
  error?: string;
  /** Offset in `text` where the currently streaming message starts (for message_replace). */
  msgStart?: number;
};

export type ChatState = {
  turns: Turn[];
  proposals: Record<string, Proposal>;
  running: boolean;
};
