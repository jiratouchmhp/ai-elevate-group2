import { deriveFlow, systemOf, type FlowInput, type NodeId, type NodeState } from "../agui/flow";

type Pos = { x: number; y: number; title: string; sub: string; tone: "gate" | "agent" | "system" };

// Vertical layout sized for the 320px side panel (viewBox units ≈ CSS px).
const W = 300;
const H = 318;
const NW = 90;
const NH = 36;
const POS: Record<NodeId, Pos> = {
  guard: { x: 150, y: 22, title: "Guardrails", sub: "Model Armor · SDP", tone: "gate" },
  orch: { x: 150, y: 84, title: "Orchestrator", sub: "Gemini · routes", tone: "agent" },
  policy_agent: { x: 52, y: 150, title: "Policy", sub: "specialist", tone: "agent" },
  workweek_agent: { x: 150, y: 150, title: "WorkWeek", sub: "specialist", tone: "agent" },
  itsm_agent: { x: 248, y: 150, title: "Service desk", sub: "specialist", tone: "agent" },
  handbook: { x: 52, y: 216, title: "Handbook", sub: "RAG · cited", tone: "system" },
  pdp: { x: 199, y: 216, title: "ACL · PDP", sub: "rules · audit", tone: "gate" },
  workweek: { x: 150, y: 282, title: "WorkWeek", sub: "HCM", tone: "system" },
  itsm: { x: 248, y: 282, title: "ServiceImm.", sub: "ITSM", tone: "system" },
};

const GLYPH: Record<NodeState, string> = { idle: "", running: "", ok: "✓", wait: "⏸", bad: "!" };
const STATE_COPY: Record<NodeState, string> = {
  idle: "not used", running: "working", ok: "done", wait: "awaiting your confirmation", bad: "stopped",
};

function edgePath(a: Pos, b: Pos): string {
  const y1 = a.y + NH / 2;
  const y2 = b.y - NH / 2;
  const my = (y1 + y2) / 2;
  return `M ${a.x} ${y1} C ${a.x} ${my}, ${b.x} ${my}, ${b.x} ${y2}`;
}

const secs = (ms: number) => (ms < 100 ? `${Math.max(1, Math.round(ms))}ms` : `${(ms / 1000).toFixed(1)}s`);

/**
 * Live multi-agent topology for the current turn: guardrails → orchestrator → specialists →
 * ACL/PDP gate → systems of record. Driven purely by the AG-UI trace the BFF already streams.
 */
export function AgentFlow(props: FlowInput) {
  const flow = deriveFlow(props);
  const used = (ids: NodeId[]) => ids.filter((id) => flow.nodes[id].state !== "idle").length;
  const agents = used(["policy_agent", "workweek_agent", "itsm_agent"]);
  const systems = used(["handbook", "workweek", "itsm"]);
  // Specialist calls are sequential and wrap their backend calls, so their sum ≈ agent time.
  const totalMs = props.steps.reduce((m, s) => (systemOf(s)?.endsWith("_agent") && s.latencyMs ? m + s.latencyMs : m), 0);

  const summary = !props.started
    ? "Idle — ask something to see the agents work."
    : props.refusal
      ? "Stopped at the guardrails — nothing was looked up or submitted."
      : `${agents} specialist${agents === 1 ? "" : "s"} · ${systems} system${systems === 1 ? "" : "s"}` +
        (props.running ? " · working…" : totalMs ? ` · ${secs(totalMs)}` : "");

  const a11y = (Object.keys(POS) as NodeId[])
    .filter((id) => flow.nodes[id].state !== "idle")
    .map((id) => `${POS[id].title} ${POS[id].sub}: ${STATE_COPY[flow.nodes[id].state]}`)
    .join("; ");

  return (
    <figure className="flow">
      <svg viewBox={`0 0 ${W} ${H}`} className="flow__svg" role="img" aria-labelledby="flow-title">
        <title id="flow-title">{`Agent flow. ${summary}${a11y ? ` ${a11y}.` : ""}`}</title>
        <defs>
          <marker id="flow-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" className="flow__arrow" />
          </marker>
        </defs>
        {flow.edges.map((e) => (
          <path key={`${e.from}-${e.to}`} d={edgePath(POS[e.from], POS[e.to])}
            className={`flow__edge flow__edge--${e.state}`} markerEnd="url(#flow-arrow)" />
        ))}
        {(Object.keys(POS) as NodeId[]).map((id) => {
          const p = POS[id];
          const n = flow.nodes[id];
          const sub = n.latencyMs !== undefined
            ? `${secs(n.latencyMs)}${n.calls > 1 ? ` · ${n.calls} calls` : ""}`
            : p.sub;
          return (
            <g key={id} className={`flow__node flow__node--${n.state} flow__node--${p.tone}`}
              transform={`translate(${p.x - NW / 2} ${p.y - NH / 2})`}>
              {n.state === "running" && <rect className="flow__pulse" width={NW} height={NH} rx={10} />}
              <rect className="flow__shadow" x={3} y={3} width={NW} height={NH} rx={10} />
              <rect className="flow__box" width={NW} height={NH} rx={10} />
              <text className="flow__title" x={NW / 2} y={15} textAnchor="middle">{p.title}</text>
              <text className="flow__sub" x={NW / 2} y={28} textAnchor="middle">{sub}</text>
              {GLYPH[n.state] && (
                <g transform={`translate(${NW - 4} 4)`}>
                  <circle className="flow__badge" r={7} />
                  <text className="flow__glyph" textAnchor="middle" y={3.5}>{GLYPH[n.state]}</text>
                </g>
              )}
            </g>
          );
        })}
      </svg>
      <figcaption className="flow__caption">{summary}</figcaption>
    </figure>
  );
}
