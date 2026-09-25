// Derives the Agent Flow graph state (nodes + edges) from one assistant turn's trace.
// Pure and framework-free so it is unit-testable; rendered by components/AgentFlow.tsx.
import type { FlowSystem, GuardrailBlock, TraceStep } from "./types";

export type NodeId =
  | "guard" | "orch" | "policy_agent" | "workweek_agent" | "itsm_agent" | "handbook" | "pdp" | "workweek" | "itsm";
export type NodeState = "idle" | "running" | "ok" | "wait" | "bad";

export type FlowNode = { id: NodeId; state: NodeState; calls: number; latencyMs?: number };
export type FlowEdge = { from: NodeId; to: NodeId; state: NodeState };
export type Flow = { nodes: Record<NodeId, FlowNode>; edges: FlowEdge[] };

export const NODE_IDS: NodeId[] = [
  "guard", "orch", "policy_agent", "workweek_agent", "itsm_agent", "handbook", "pdp", "workweek", "itsm",
];

export const EDGES: [NodeId, NodeId][] = [
  ["guard", "orch"],
  ["orch", "policy_agent"], ["orch", "workweek_agent"], ["orch", "itsm_agent"],
  ["policy_agent", "handbook"], ["workweek_agent", "pdp"], ["itsm_agent", "pdp"],
  ["pdp", "workweek"], ["pdp", "itsm"],
];

// Fallback when the BFF predates the `system` tag (keep in sync with TOOL_SYSTEM in app/ui_events.py).
const NAME_SYSTEM: Record<string, FlowSystem> = {
  policy_agent: "policy_agent", workweek_agent: "workweek_agent", itsm_agent: "itsm_agent",
  search_policy: "handbook",
  get_profile: "workweek", get_personal_info: "workweek", get_leave_balance: "workweek", get_leave_requests: "workweek",
  propose_leave: "workweek", propose_cancel_leave: "workweek", propose_contact_update: "workweek",
  list_tickets: "itsm", get_ticket: "itsm", propose_incident: "itsm", propose_comment: "itsm",
  propose_status_update: "itsm",
};

export function systemOf(step: TraceStep): FlowSystem | null {
  if (step.system) return step.system;
  if (step.name === "commit_action") {
    return step.agent === "workweek_agent" ? "workweek" : step.agent === "itsm_agent" ? "itsm" : null;
  }
  return NAME_SYSTEM[step.name] ?? null;
}

const BAD = new Set(["denied", "failed", "unavailable", "error"]);

export function stepState(status: string): NodeState {
  if (status === "running") return "running";
  if (BAD.has(status)) return "bad";
  if (status === "awaiting_confirmation") return "wait";
  return "ok";
}

// running beats everything (it's live); then bad > wait > ok > idle.
const RANK: Record<NodeState, number> = { idle: 0, ok: 1, wait: 2, bad: 3, running: 4 };
const worst = (a: NodeState, b: NodeState): NodeState => (RANK[b] > RANK[a] ? b : a);

/** The route each step lights up, as the chain of nodes from the orchestrator outward. */
function pathOf(step: TraceStep, sys: FlowSystem): NodeId[] {
  switch (sys) {
    case "policy_agent":
    case "workweek_agent":
    case "itsm_agent":
      return ["orch", sys];
    case "handbook":
      return ["orch", "policy_agent", "handbook"];
    case "workweek":
    case "itsm": {
      // Reads and writes go specialist -> ACL/PDP -> system of record.
      const agent: NodeId = step.agent === "itsm_agent" || step.agent === "workweek_agent"
        ? step.agent
        : sys === "itsm" ? "itsm_agent" : "workweek_agent";
      return ["orch", agent, "pdp", sys];
    }
  }
}

/**
 * State of one hop on a step's route. Specialist / gate hops the request passed through are "ok"
 * unless the step is still running; the PDP gate owns denials (the system of record is then never
 * reached -> null); the destination owns outages and "awaiting confirmation".
 */
function hopState(hop: NodeId, target: NodeId, st: NodeState, status: string): NodeState | null {
  if (st === "running") return "running";
  if (st === "bad" && status === "denied") {
    if (hop === "pdp") return "bad";
    return hop === target ? null : "ok";
  }
  return hop === target ? st : "ok";
}

export type FlowInput = { steps: TraceStep[]; running: boolean; started: boolean; refusal?: GuardrailBlock; error?: boolean };

export function deriveFlow({ steps, running, started, refusal, error }: FlowInput): Flow {
  const nodes = Object.fromEntries(NODE_IDS.map((id) => [id, { id, state: "idle", calls: 0 } as FlowNode])) as Record<NodeId, FlowNode>;
  const edgeState = new Map<string, NodeState>();
  const key = (a: NodeId, b: NodeId) => `${a}>${b}`;

  if (started) {
    nodes.guard.state = refusal ? "bad" : "ok";
    if (!refusal) {
      nodes.orch.state = running ? "running" : error ? "bad" : "ok";
      edgeState.set(key("guard", "orch"), nodes.orch.state);
    }
  }

  for (const step of steps) {
    const sys = systemOf(step);
    if (!sys) continue;
    const st = stepState(step.status);
    const path = pathOf(step, sys);
    const targetId = path[path.length - 1];
    nodes[targetId].calls += 1;
    if (step.latencyMs !== undefined && step.status !== "running") nodes[targetId].latencyMs = step.latencyMs;
    for (let i = 1; i < path.length; i++) {
      const hop = hopState(path[i], targetId, st, step.status);
      if (hop === null) break; // not reached (e.g. PDP denied before the system of record)
      nodes[path[i]].state = worst(nodes[path[i]].state, hop);
      const e = key(path[i - 1], path[i]);
      edgeState.set(e, worst(edgeState.get(e) ?? "idle", hop));
    }
  }

  // Once the run is over nothing is "running" any more (a dropped stream shouldn't spin forever).
  if (!running) {
    for (const n of Object.values(nodes)) if (n.state === "running") n.state = error ? "bad" : "ok";
    for (const [k, v] of edgeState) if (v === "running") edgeState.set(k, error ? "bad" : "ok");
  }

  return { nodes, edges: EDGES.map(([from, to]) => ({ from, to, state: edgeState.get(key(from, to)) ?? "idle" })) };
}
