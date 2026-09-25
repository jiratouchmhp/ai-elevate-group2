"""Model-free eval layers: retrieval Recall@5/MRR and input-guardrail detection/FP rates.

    uv run python tests/eval/scripts/eval_offline_layers.py [--md artifacts/offline_layers.md]

Layer 1 (retrieval) runs the production retriever on every labelled policy question.
The guardrail layer runs the deterministic input path (SPII redaction -> classifier)
over the red-team set and the benign golden set. No LLM is involved, so these numbers
are exact and reproducible; they isolate WHERE failures originate (SDD §9.1).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HR_FIXED_TODAY", "2026-10-05")
for k in ("MODEL_ARMOR_TEMPLATE_ID", "MODEL_ARMOR_TEMPLATE_ID_INPUT", "MODEL_ARMOR_TEMPLATE_ID_OUTPUT"):
    os.environ.pop(k, None)

DS = ROOT / "tests" / "eval" / "datasets"
ANCHOR = re.compile(r"^s(\d+(?:\.\d+)*)-")


def _text(case):
    if case.get("prompt"):
        return "".join(p.get("text", "") for p in case["prompt"]["parts"])
    return " ".join(p.get("text", "") for t in case["agent_data"]["turns"] for e in t["events"]
                    if e["author"] == "user" for p in e["content"]["parts"])


def _match(got: str, want: str) -> bool:
    return got == want or got.startswith(want + ".") or want.startswith(got + ".")


def retrieval(cases, k=5):
    from app.policy.retriever import get_retriever

    r = get_retriever()
    rows, by_cat = [], defaultdict(lambda: [0, 0, 0.0])
    for c in cases:
        want = c.get("expected", {}).get("sections") or []
        if not want:
            continue
        hits = r.search(_text(c), top_k=k)
        secs = [m.group(1) for h in hits if (m := ANCHOR.match(h.chunk.anchor))]
        rank = next((i + 1 for i, s in enumerate(secs) if any(_match(s, w) for w in want)), None)
        cat = c["category"]
        by_cat[cat][0] += 1
        by_cat[cat][1] += rank is not None
        by_cat[cat][2] += (1 / rank) if rank else 0
        rows.append((c["eval_case_id"], cat, want, secs, rank))
    return rows, by_cat


def guardrail(cases):
    from app.guardrails import spii
    from app.guardrails.classifier import CompositeClassifier

    clf = CompositeClassifier()
    out = []
    for c in cases:
        red, _ = spii.redact(_text(c), phones=False)
        v = clf.classify(red, "input")
        out.append((c["eval_case_id"], c["category"], c["expected"].get("outcome"), v.blocked, v.categories))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md")
    a = ap.parse_args()
    load = lambda n: json.loads((DS / f"{n}.json").read_text())["eval_cases"]  # noqa: E731
    single, tools, multi, red = load("single_turn"), load("tool_calling"), load("multi_turn"), load("redteam")

    from app.policy.retriever import get_retriever

    md = [f"## Layer 1 · Retrieval ({type(get_retriever()).__name__}, top-5, no LLM)", "",
          "| Category | N | Recall@5 | MRR |", "|---|---|---|---|"]
    rows, by_cat = retrieval(single + tools + [c for c in red if c["category"] == "redteam.false_positive"])
    tot = [0, 0, 0.0]
    for cat, (n, hit, rr) in sorted(by_cat.items()):
        md.append(f"| {cat} | {n} | {hit / n:.1%} | {rr / n:.3f} |")
        tot = [tot[0] + n, tot[1] + hit, tot[2] + rr]
    md.append(f"| **all labelled** | **{tot[0]}** | **{tot[1] / tot[0]:.1%}** | **{tot[2] / tot[0]:.3f}** |")
    misses = [r for r in rows if r[4] is None]
    md += ["", f"Misses ({len(misses)}):", ""]
    md += [f"- `{cid}` [{cat}] want {want}, got top-5 {secs}" for cid, cat, want, secs, _ in misses]

    g_red = guardrail(red)
    g_benign = guardrail(single + tools + [c for c in multi if not c["category"].startswith("sec.")])
    md += ["", "## Input guardrail (SPII redaction -> local classifier; Model Armor not configured)", "",
           "| Category | N | Blocked at input | Expected outcome |", "|---|---|---|---|"]
    agg = defaultdict(lambda: [0, 0, set()])
    for cid, cat, exp, blocked, _ in g_red:
        agg[cat][0] += 1
        agg[cat][1] += blocked
        agg[cat][2].add(exp)
    for cat, (n, b, exps) in sorted(agg.items()):
        md.append(f"| {cat} | {n} | {b} ({b / n:.0%}) | {', '.join(sorted(exps))} |")
    fp_probe = [x for x in g_red if x[1] == "redteam.false_positive" and x[3]]
    fp_benign = [x for x in g_benign if x[3]]
    n_fp = len([x for x in g_red if x[1] == "redteam.false_positive"])
    md += ["", f"False positives on the FP-probe set: {len(fp_probe)}/{n_fp} = {len(fp_probe) / n_fp:.1%}",
           f"False positives on the benign golden/tool/multi-turn prompts: {len(fp_benign)}/{len(g_benign)} "
           f"= {len(fp_benign) / max(1, len(g_benign)):.1%}", ""]
    md += [f"- FP `{cid}` [{cat}] categories={cats}" for cid, cat, _, _, cats in fp_probe + fp_benign]
    missed = [x for x in g_red if x[2] in ("blocked", "wellbeing") and not x[3]]
    md += ["", f"Attack prompts expecting `blocked`/`wellbeing` that pass the input classifier "
               f"(must be refused by the model): {len(missed)}", ""]
    md += [f"- `{cid}` [{cat}]" for cid, cat, *_ in missed]
    text = "\n".join(md)
    print(text)
    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text(text)


if __name__ == "__main__":
    main()
