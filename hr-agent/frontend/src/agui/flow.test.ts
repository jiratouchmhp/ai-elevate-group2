import { describe, expect, it } from "vitest";
import { deriveFlow, systemOf } from "./flow";
import { initialState, reducer, type Action } from "./reducer";
import type { AguiEvent, TraceStep, Widget } from "./types";

const step = (s: Partial<TraceStep> & Pick<TraceStep, "name" | "status">): TraceStep => ({
  id: s.name, label: s.name, agent: "hr_agent", ...s,
});

describe("deriveFlow", () => {
  it("is idle before any turn", () => {
    const f = deriveFlow({ steps: [], running: false, started: false });
    expect(Object.values(f.nodes).every((n) => n.state === "idle")).toBe(true);
    expect(f.edges.every((e) => e.state === "idle")).toBe(true);
  });

  it("lights the cross-system route and animates the live hop (UC-2.1)", () => {
    const f = deriveFlow({
      running: true, started: true,
      steps: [
        step({ name: "policy_agent", status: "done", system: "policy_agent", latencyMs: 900 }),
        step({ name: "search_policy", status: "success", agent: "policy_agent", system: "handbook", latencyMs: 120 }),
        step({ name: "workweek_agent", status: "running", system: "workweek_agent" }),
        step({ name: "get_profile", status: "running", agent: "workweek_agent", system: "workweek" }),
      ],
    });
    expect(f.nodes.guard.state).toBe("ok");
    expect(f.nodes.orch.state).toBe("running");
    expect(f.nodes.policy_agent.state).toBe("ok");
    expect(f.nodes.handbook.state).toBe("ok");
    expect(f.nodes.handbook.latencyMs).toBe(120);
    expect(f.nodes.workweek_agent.state).toBe("running");
    expect(f.nodes.pdp.state).toBe("running");
    expect(f.nodes.workweek.state).toBe("running");
    expect(f.nodes.itsm.state).toBe("idle");
    expect(f.edges.find((e) => e.from === "pdp" && e.to === "workweek")?.state).toBe("running");
    expect(f.edges.find((e) => e.from === "pdp" && e.to === "itsm")?.state).toBe("idle");
  });

  it("a PDP denial stops at the gate; the system of record is never reached", () => {
    const f = deriveFlow({
      running: false, started: true,
      steps: [step({ name: "propose_leave", status: "denied", agent: "workweek_agent", system: "workweek" })],
    });
    expect(f.nodes.pdp.state).toBe("bad");
    expect(f.nodes.workweek_agent.state).toBe("ok");
    expect(f.nodes.workweek.state).toBe("idle");
  });

  it("awaiting confirmation is amber at the destination; outages are red there", () => {
    const f = deriveFlow({
      running: false, started: true,
      steps: [
        step({ name: "propose_incident", status: "awaiting_confirmation", agent: "itsm_agent", system: "itsm" }),
        step({ name: "get_leave_balance", status: "unavailable", agent: "workweek_agent", system: "workweek" }),
      ],
    });
    expect(f.nodes.itsm.state).toBe("wait");
    expect(f.nodes.workweek.state).toBe("bad");
    expect(f.nodes.pdp.state).toBe("ok");
  });

  it("a guardrail refusal stops everything at the first node", () => {
    const f = deriveFlow({ steps: [], running: false, started: true, refusal: { categories: ["jailbreak"], wellbeing: false } });
    expect(f.nodes.guard.state).toBe("bad");
    expect(f.nodes.orch.state).toBe("idle");
  });

  it("settles stale running nodes once the run ends, and falls back to tool names without `system`", () => {
    const f = deriveFlow({ steps: [step({ name: "list_tickets", status: "running", agent: "itsm_agent" })], running: false, started: true });
    expect(f.nodes.itsm.state).toBe("ok");
    expect(systemOf(step({ name: "commit_action", status: "committed", agent: "workweek_agent" }))).toBe("workweek");
  });
});

describe("reducer widgets", () => {
  const ev = (e: AguiEvent): Action => ({ kind: "event", ev: e });
  const w = (id: string, data: Widget["data"], kind: Widget["kind"] = "leave_balance"): AguiEvent =>
    ({ type: "CUSTOM", name: "widget", value: { id, kind, agent: "workweek_agent", data } as Widget });

  it("attaches widgets to the current turn; a re-read replaces the same kind", () => {
    const s = [
      { kind: "user", id: "1", text: "balance?" } as Action,
      ev(w("c1", { balances: [{ type: "Vacation", accrued: 21, used: 16, pending: null, remaining: 5 }] })),
      ev(w("c2", { balances: [{ type: "Vacation", accrued: 21, used: 17, pending: null, remaining: 4 }] })),
      ev(w("c3", { tickets: [] }, "ticket_list")),
      ev(w("c4", { ticket_id: "INC1", comments: [] }, "ticket_detail")),
      ev(w("c5", { ticket_id: "INC2", comments: [] }, "ticket_detail")),
    ].reduce(reducer, initialState);
    const widgets = s.turns[1].widgets;
    expect(widgets.map((x) => x.id)).toEqual(["c2", "c3", "c4", "c5"]);
  });

  it("carries the flow system onto trace steps", () => {
    const s = [
      { kind: "user", id: "1", text: "hi" } as Action,
      ev({ type: "TOOL_CALL_START", toolCallId: "t", toolCallName: "get_profile", label: "x", agent: "workweek_agent", system: "workweek" }),
    ].reduce(reducer, initialState);
    expect(s.turns[1].trace[0].system).toBe("workweek");
  });
});
