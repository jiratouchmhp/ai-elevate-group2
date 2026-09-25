.PHONY: test lint eval-offline eval-rag eval-traces eval-grade gate package-eval run-acl run ui-install ui-dev ui-build ui-test \
	tf-validate tf-init tf-plan deploy redeploy rag-sync push-secrets smoke

# Unit + offline integration tests (no model, no network) with the PDP 100% branch gate.
test:
	uv run pytest tests/unit tests/integration/test_confirmation_flow.py tests/integration/test_eval_harness.py \
		tests/integration/test_acl_service.py tests/integration/test_mcp_backend.py tests/integration/test_ui_bff.py tests/integration/test_http_pool.py -q --cov=app --cov-branch
	uv run coverage report --include="app/integration/pdp.py" --fail-under=100

lint:
	uv run --with ruff ruff check app acl_service mock_backends

# Model-free eval layers: retrieval Recall@5/MRR + input guardrail detection / FP rates.
eval-offline:
	uv run python tests/eval/scripts/eval_offline_layers.py --md artifacts/offline_layers.md

# Same retrieval layer against the DEPLOYED Vertex AI RAG Engine corpus (read-only; needs ADC).
# Use it to re-calibrate RAG_DISTANCE_THRESHOLD whenever the corpus or embedding model changes.
RAG_CORPUS_TF := $(shell sed -n 's/^rag_corpus *= *"\(.*\)"/\1/p' deployment/terraform/terraform.tfvars 2>/dev/null)
eval-rag:
	@test -n "$(RAG_CORPUS_TF)" || (echo "rag_corpus not set in terraform.tfvars (run make rag-sync)"; exit 1)
	RAG_CORPUS=$(RAG_CORPUS_TF) GOOGLE_CLOUD_PROJECT=$$(echo $(RAG_CORPUS_TF) | cut -d/ -f2) \
		uv run python tests/eval/scripts/eval_offline_layers.py --md artifacts/offline_layers_rag.md

# End-to-end traces with live Gemini (needs ADC: gcloud auth application-default login).
eval-traces:
	uv run python tests/eval/generate_traces.py

eval-grade:
	agents-cli eval grade --traces artifacts/traces/ --config tests/eval/eval_config.yaml --qps 5

# Local release gate (replaces the CI gate for MVP): tests, offline layers, then thresholds
# on the latest graded results. Exit code 1 = hard block, 2 = release threshold miss.
gate: test eval-offline
	uv run python tests/eval/scripts/eval_gate.py $$(ls -t artifacts/grade_results/results_*.json | head -1) \
		--md artifacts/gate_summary.md

package-eval:
	bash scripts/package_eval.sh artifacts/eval_suite.zip

run-acl:
	uv run uvicorn acl_service.main:app --port 8081

# --- Chat UI (React + Vite, SDD §3.10) -------------------------------------------
ui-install:
	cd frontend && npm ci --no-fund --no-audit

# Hot-reload UI on :5173 (proxies /api to `make run` on :8000).
ui-dev:
	cd frontend && npm run dev

ui-build:
	cd frontend && npm run build

ui-test:
	cd frontend && npm run typecheck && npm test

# Agent + BFF + built UI on http://localhost:8000 (ADK dev UI stays at /dev-ui/).
# UI_DEV_PERSONAS=1 enables the mock-persona switcher (ignored with a shared PAT / behind IAP).
run:
	UI_DEV_PERSONAS=1 uv run uvicorn app.fast_api_app:app --port 8000 --reload

# --- GCP deploy (deployment/terraform + deployment/deploy.sh) --------------------
# One-time: cp deployment/terraform/terraform.tfvars.example deployment/terraform/terraform.tfvars
tf-validate:
	terraform -chdir=deployment/terraform fmt -check -recursive
	terraform -chdir=deployment/terraform init -backend=false -input=false >/dev/null
	terraform -chdir=deployment/terraform validate

tf-init:
	bash deployment/deploy.sh bootstrap

tf-plan:
	bash deployment/deploy.sh plan

# Full pipeline: bootstrap -> infra -> push-secrets -> rag-sync -> build -> apply -> smoke
deploy:
	bash deployment/deploy.sh all

# Re-deploy code only (new images + apply).
redeploy:
	bash deployment/deploy.sh build && bash deployment/deploy.sh apply && bash deployment/deploy.sh smoke

rag-sync:
	bash deployment/deploy.sh rag-sync

push-secrets:
	bash deployment/deploy.sh push-secrets

smoke:
	bash deployment/deploy.sh smoke
