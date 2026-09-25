"""Apply SDD §9.4 thresholds to an `agents-cli eval grade` results file.

    uv run python tests/eval/scripts/eval_gate.py artifacts/grade_results/results_<ts>.json [--md out.md]

Reads `thresholds` from tests/eval/eval_config.yaml, computes each metric over the
APPLICABLE cases (score != None) optionally filtered by category `scope`, prints a
table plus every failing case with the metric's explanation (the RCA input), and exits
1 if any `hard_block` threshold fails (2 if only `release` thresholds fail).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def _cases(results: dict) -> list[dict]:
    out = []
    for ds in results.get("evaluation_dataset") or []:
        out.extend(ds.get("eval_cases") or [])
    return out


def load(results_path: Path) -> tuple[list[dict], dict[str, list[tuple[dict, dict]]]]:
    results = json.loads(results_path.read_text())
    cases = _cases(results)
    per_metric: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for r in results["eval_case_results"]:
        case = cases[r["eval_case_index"]] if r["eval_case_index"] < len(cases) else {}
        for cand in r.get("response_candidate_results") or []:
            for name, m in (cand.get("metric_results") or {}).items():
                per_metric[name].append((case, m))
    return cases, per_metric


def _in_scope(case: dict, spec: dict) -> bool:
    cat = case.get("category") or ""
    scope = spec.get("scope")
    if scope and not cat.startswith(tuple(scope) if isinstance(scope, list) else scope):
        return False
    if spec.get("exclude") and cat.startswith(spec["exclude"]):
        return False
    return True


def _p95(vals: list[float]) -> float:
    s = sorted(vals)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def evaluate(per_metric, thresholds: dict) -> list[dict]:
    rows = []
    for name, spec in thresholds.items():
        source = spec.get("source", name)
        pairs = [(c, m) for c, m in per_metric.get(source, []) if _in_scope(c, spec)]
        graded = [(c, m) for c, m in pairs if m.get("score") is not None]
        errors = [(c, m) for c, m in pairs if m.get("score") is None
                  and not str(m.get("explanation") or "").startswith("N/A")]
        row = {"name": name, "gate": spec.get("gate"), "n": len(graded), "errors": len(errors), "failures": []}
        if not graded:
            row.update(value=None, target="—", passed=None)
            rows.append(row)
            continue
        scores = [float(m["score"]) for _, m in graded]
        if spec.get("aggregate") == "p95":
            value = _p95(scores)
            row.update(value=value, target=f"< {spec['max']}", passed=value < spec["max"],
                       failures=[(c, m) for c, m in graded if float(m["score"]) >= spec["max"]])
        elif "max_count" in spec:
            fails = [(c, m) for c, m in graded if float(m["score"]) < 1.0]
            row.update(value=len(fails), target=f"= {spec['max_count']}", passed=len(fails) <= spec["max_count"],
                       failures=fails)
        else:
            fails = [(c, m) for c, m in graded if float(m["score"]) < 1.0]
            rate = 1 - len(fails) / len(graded)
            if spec.get("invert"):
                value = 1 - rate
                row.update(value=value, target=f"< {spec['max']:.0%}", passed=value < spec["max"] or value == 0)
            else:
                value = rate
                row.update(value=value, target=f"≥ {spec['min']:.0%}", passed=value >= spec["min"])
            row["failures"] = fails
        rows.append(row)
    return rows


def render(rows, cases) -> str:
    by_suite = defaultdict(int)
    for c in cases:
        by_suite[(c.get("category") or "?").split(".")[0]] += 1
    lines = [f"Cases graded: {len(cases)} ({', '.join(f'{k} {v}' for k, v in sorted(by_suite.items()))})", "",
             "| Metric | Gate | N | Value | Target | Result |", "|---|---|---|---|---|---|"]
    for r in rows:
        v = r["value"]
        vs = "—" if v is None else (f"{v:.1%}" if isinstance(v, float) and "p95" not in r["name"] else
                                    (f"{v:.2f}s" if "p95" in r["name"] else str(v)))
        res = "n/a" if r["passed"] is None else ("PASS" if r["passed"] else "**FAIL**")
        err = f" (+{r['errors']} err)" if r["errors"] else ""
        lines.append(f"| {r['name']} | {r['gate']} | {r['n']}{err} | {vs} | {r['target']} | {res} |")
    lines += ["", "### Failing cases", ""]
    seen = set()
    for r in rows:
        for c, m in r["failures"]:
            key = (r["name"], c.get("eval_case_id"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- `{c.get('eval_case_id')}` [{c.get('category')}] **{r['name']}**: "
                         f"{str(m.get('explanation') or '')[:300]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--config", default=str(ROOT / "tests" / "eval" / "eval_config.yaml"))
    ap.add_argument("--md", help="write the markdown summary here")
    a = ap.parse_args()
    thresholds = yaml.safe_load(Path(a.config).read_text())["thresholds"]
    cases, per_metric = load(Path(a.results))
    rows = evaluate(per_metric, thresholds)
    md = render(rows, cases)
    print(md)
    if a.md:
        Path(a.md).write_text(md)
    hard = [r for r in rows if r["gate"] == "hard_block" and r["passed"] is False]
    rel = [r for r in rows if r["gate"] == "release" and r["passed"] is False]
    print(f"\nGATE: {'BLOCKED' if hard else ('RELEASE-FAIL' if rel else 'PASS')} "
          f"(hard_block failures: {len(hard)}, release failures: {len(rel)})")
    return 1 if hard else (2 if rel else 0)


if __name__ == "__main__":
    sys.exit(main())
