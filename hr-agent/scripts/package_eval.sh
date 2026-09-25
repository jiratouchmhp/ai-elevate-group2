#!/usr/bin/env bash
# Build eval_suite.zip (rooted at tests/eval/) for the Agent Evaluation Suite deliverable.
#   bash scripts/package_eval.sh [out.zip]
# Excludes caches and generated artifacts; fails if the archive is >= 50 MB or a secret-looking
# string is found.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-artifacts/eval_suite.zip}"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

required=(tests/eval/datasets/single_turn.json tests/eval/datasets/multi_turn.json
          tests/eval/eval_config.yaml tests/eval/evaluation_report.md)
for f in "${required[@]}"; do [[ -f "$f" ]] || { echo "missing required file: $f" >&2; exit 1; }; done

for f in tests/eval/datasets/*.json; do python3 -m json.tool "$f" >/dev/null || { echo "invalid JSON: $f" >&2; exit 1; }; done

if grep -rEn "mcp_[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|BEGIN (RSA|EC|PRIVATE) KEY" tests/eval; then
  echo "secret-like string found in tests/eval — refusing to package" >&2; exit 1
fi

zip -qr "$OUT" tests/eval -x "*/__pycache__/*" "*.pyc" "*/.DS_Store"
size=$(wc -c < "$OUT" | tr -d ' ')
if (( size >= 50 * 1024 * 1024 )); then echo "archive too large: $size bytes" >&2; exit 1; fi
echo "wrote $OUT ($size bytes)"
unzip -l "$OUT"
