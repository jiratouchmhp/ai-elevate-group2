# ==============================================================================
# Altostrat Singapore — HR Agentic Assistant (MVP 1) Makefile
# Target GCP Project: ai-training-van-01 (Region: asia-southeast1)
# ==============================================================================

export PATH      := /usr/local/google/home/vannick/.local/bin:/usr/local/google/home/vannick/google-cloud-sdk/bin:$(PATH)

-include .env
export

PROJECT_ID       ?= ai-training-van-01
REGION           ?= asia-southeast1
GOOGLE_CLOUD_LOCATION ?= global
VERTEX_RAG_LOCATION ?= $(REGION)
GEMINI_MODEL     ?= gemini-3.8-flash
GEMINI_PRO_MODEL ?= $(GEMINI_MODEL)
GEMINI_FLASH_MODEL ?= $(GEMINI_MODEL)
AR_REPO          ?= hr-agent
IMAGE_TAG        ?= 1.0.0
PORT             ?= 8080
ADK_PORT         ?= 8000
VENV             ?= .venv
ADK_TOOL_PYTHON  ?= /usr/local/google/home/vannick/.local/share/uv/tools/google-adk/bin/python
PYTHON           := $(shell if [ -x "$(ADK_TOOL_PYTHON)" ]; then echo "$(ADK_TOOL_PYTHON)"; elif [ -x "$(VENV)/bin/python" ] && "$(VENV)/bin/python" -c "import yaml" >/dev/null 2>&1; then echo "$(VENV)/bin/python"; else echo "python3"; fi)
PIP              := $(if $(wildcard $(VENV)/bin/pip),$(VENV)/bin/pip,pip3)
TF_DIR           ?= deployment/terraform/single-project
RAG_OUTPUT_DIR   ?= build/rag
RAG_BUCKET_URI   := gs://$(PROJECT_ID)-hr-policy-corpus
RAG_DATASTORE_ID ?= altostrat-sg-policy-handbook-ds
VERTEX_RAG_CORPUS_ID ?= 4611686018427387904
USE_CLOUD_RAG    ?= true
GOOGLE_GENAI_USE_VERTEXAI ?= TRUE
MCP_SERVER_BASE_URL ?= https://mock-saas.aishprabhat.demo.altostrat.com
WORKWEEK_MCP_URL ?= $(MCP_SERVER_BASE_URL)/work-week/mcp/
SERVICE_IMMEDIATELY_MCP_URL ?= $(MCP_SERVER_BASE_URL)/service-immediately/mcp/
MCP_AUTHENTICATED_EMPLOYEE_ID ?= EMP-836
USE_LIVE_MCP     ?= true
AR_IMAGE_PREFIX  := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)

.DEFAULT_GOAL := help

.PHONY: help install start start-ui run start-adk local-adk start-local dev local-up \
        test-local local-user-test test \
        mcp-test mcp-unit-test mcp-integration-test mcp-e2e-test \
        rag-ingest rag-test test-retrieval rag-agent-test rag-provision \
        gcp-auth-check gcp-enable-apis gcp-setup-ar \
        cloud-build docker-build \
        tf-init tf-validate tf-plan tf-apply tf-destroy \
        provision

help: ## Show available targets and current GCP configuration
	@echo "=============================================================================="
	@echo " Altostrat Singapore — HR Agentic Assistant (MVP 1)"
	@echo " Target GCP Project    : $(PROJECT_ID) (Vertex AI: $(GOOGLE_GENAI_USE_VERTEXAI))"
	@echo " Vertex AI Model Loc   : $(GOOGLE_CLOUD_LOCATION) (Pro: $(GEMINI_PRO_MODEL) / Flash: $(GEMINI_FLASH_MODEL))"
	@echo " Target RAG Region     : $(VERTEX_RAG_LOCATION)"
	@echo " RAG Corpus Bucket     : $(RAG_BUCKET_URI)"
	@echo " Vertex AI RAG Corpus  : $(VERTEX_RAG_CORPUS_ID) ($(VERTEX_RAG_LOCATION))"
	@echo " MCP Server Base URL   : $(MCP_SERVER_BASE_URL)"
	@echo " MCP Auth Employee ID  : $(MCP_AUTHENTICATED_EMPLOYEE_ID)"
	@echo " Active Python / ADK   : $(PYTHON)"
	@echo "=============================================================================="
	@echo ""
	@echo "Local User Testing (UI + Google ADK + Live MCP + GCP Vertex AI RAG):"
	@echo "  make test-local       Verify UI + ADK + Live MCP ($(MCP_AUTHENTICATED_EMPLOYEE_ID)) + GCP RAG as user before deploy"
	@echo "  make start-local      Run BOTH Customer Chat UI ( :$(PORT) ) & ADK Web UI ( :$(ADK_PORT) ) locally"
	@echo "  make start            Start Customer Chat UI (FastAPI + AG-UI SSE) on http://vannick2.c.googlers.com:$(PORT)"
	@echo "  make start-adk        Start Google ADK Developer Web UI on http://vannick2.c.googlers.com:$(ADK_PORT)"
	@echo ""
	@echo "Automated Test Suites:"
	@echo "  make install          Install/sync Python dependencies via uv / pip"
	@echo "  make test             Run full unit, integration, E2E & golden eval test suite"
	@echo "  make mcp-test         Run MCP Unit + Live MCP Integration + Agent MCP E2E tests"
	@echo ""
	@echo "RAG Policy Search (Ingestion, Retrieval Test & GCP Provisioning):"
	@echo "  make rag-ingest       Parse handbook with C-1..C-6 gates -> $(RAG_OUTPUT_DIR)/policy_chunks.jsonl"
	@echo "  make rag-test         Run 7-point RAG retrieval benchmark (or pass QUERY=\"...\")"
	@echo "  make rag-agent-test   Run End-to-End ADK Agent -> Vertex AI RAG Engine test suite"
	@echo "  make test-retrieval   Alias for 'make rag-test' (supports QUERY=\"...\")"
	@echo "  make rag-provision    Provision GCS bucket + Vertex AI RAG Engine Corpus in $(PROJECT_ID)"
	@echo ""
	@echo "GCP Provisioning & Terraform (Project: $(PROJECT_ID)):"
	@echo "  make gcp-auth-check   Set gcloud project to $(PROJECT_ID) and check credentials"
	@echo "  make gcp-enable-apis  Enable required GCP APIs on $(PROJECT_ID)"
	@echo "  make gcp-setup-ar     Create Artifact Registry repository '$(AR_REPO)' in $(REGION)"
	@echo "  make cloud-build      Build & push acl-pdp and chat-ui-bff images via Cloud Build"
	@echo "  make tf-init          Initialize Terraform in $(TF_DIR)"
	@echo "  make tf-plan          Generate Terraform plan for $(PROJECT_ID)"
	@echo "  make tf-apply         Apply Terraform resources to $(PROJECT_ID)"
	@echo "  make provision        End-to-end GCP provisioning (APIs + AR + Build + Terraform + RAG)"
	@echo "=============================================================================="

# ------------------------------------------------------------------------------
# 1. Local Setup, Application Startup & Pre-Deployment User Testing
# ------------------------------------------------------------------------------
install: ## Install Python dependencies into virtual environment (supports uv and pip)
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --seed $(VENV) && uv pip install --python $(VENV)/bin/python -e ".[dev]"; \
	else \
		python3 -m venv --clear $(VENV) && $(VENV)/bin/pip install --upgrade pip && $(VENV)/bin/pip install -e ".[dev]"; \
	fi

start: ## Start the Customer Chat UI (FastAPI + AG-UI BFF) locally on PORT (default: 8080)
	@echo ">>> Starting Customer Chat UI (AG-UI BFF) on http://vannick2.c.googlers.com:$(PORT)"
	@echo ">>> Connected to GCP RAG Corpus $(VERTEX_RAG_CORPUS_ID) ($(VERTEX_RAG_LOCATION)) + Live MCP ($(MCP_AUTHENTICATED_EMPLOYEE_ID))"
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
	VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
	GEMINI_MODEL=$(GEMINI_MODEL) \
	GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
	GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
	GOOGLE_GENAI_USE_VERTEXAI=$(GOOGLE_GENAI_USE_VERTEXAI) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=$(USE_CLOUD_RAG) \
	USE_LIVE_MCP=$(USE_LIVE_MCP) \
	MCP_AUTHENTICATED_EMPLOYEE_ID=$(MCP_AUTHENTICATED_EMPLOYEE_ID) \
	$(PYTHON) -m app.ui.ag_ui_server --host 0.0.0.0 --port $(PORT)

start-ui: start ## Alias for 'make start'

run: start ## Alias for 'make start'

start-adk: ## Launch the Google ADK Developer Web UI on ADK_PORT (default: 8000)
	@echo ">>> Starting Google ADK Developer Web UI on http://vannick2.c.googlers.com:$(ADK_PORT)"
	@echo ">>> Select agent 'app' (altostrat_hr_agent) — Model: $(GEMINI_PRO_MODEL) ($(GOOGLE_CLOUD_LOCATION)) + GCP RAG ($(VERTEX_RAG_LOCATION)) + Live MCP ($(MCP_AUTHENTICATED_EMPLOYEE_ID))"
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
	VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
	GEMINI_MODEL=$(GEMINI_MODEL) \
	GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
	GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
	GOOGLE_GENAI_USE_VERTEXAI=$(GOOGLE_GENAI_USE_VERTEXAI) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=$(USE_CLOUD_RAG) \
	USE_LIVE_MCP=$(USE_LIVE_MCP) \
	MCP_AUTHENTICATED_EMPLOYEE_ID=$(MCP_AUTHENTICATED_EMPLOYEE_ID) \
	$(PYTHON) -m google.adk.cli web . --host 0.0.0.0 --port $(ADK_PORT)

local-adk: start-adk ## Alias for 'make start-adk'

start-local: ## Start BOTH Customer Chat UI (:8080) and Google ADK Web UI (:8000) locally with Live MCP & GCP RAG
	@echo "=============================================================================="
	@echo " Launching Local Pre-Deployment Environment (Live MCP + GCP Vertex AI RAG)"
	@echo "   1. Customer Chat UI (AG-UI BFF) : http://vannick2.c.googlers.com:$(PORT)"
	@echo "   2. Google ADK Developer Web UI  : http://vannick2.c.googlers.com:$(ADK_PORT)"
	@echo "   3. Vertex AI Gemini Model       : $(GEMINI_PRO_MODEL) (Location: $(GOOGLE_CLOUD_LOCATION))"
	@echo "   4. GCP Vertex AI RAG Corpus     : $(VERTEX_RAG_CORPUS_ID) ($(PROJECT_ID) / $(VERTEX_RAG_LOCATION))"
	@echo "   5. Live Vendor MCP Server       : $(MCP_SERVER_BASE_URL) (User: $(MCP_AUTHENTICATED_EMPLOYEE_ID))"
	@echo " Press Ctrl+C to stop both servers."
	@echo "=============================================================================="
	@bash -c 'trap "kill 0" INT TERM EXIT; \
		GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
		GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
		VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
		GEMINI_MODEL=$(GEMINI_MODEL) \
		GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
		GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
		GOOGLE_GENAI_USE_VERTEXAI=$(GOOGLE_GENAI_USE_VERTEXAI) \
		VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
		USE_CLOUD_RAG=$(USE_CLOUD_RAG) \
		USE_LIVE_MCP=$(USE_LIVE_MCP) \
		MCP_AUTHENTICATED_EMPLOYEE_ID=$(MCP_AUTHENTICATED_EMPLOYEE_ID) \
		$(PYTHON) -m app.ui.ag_ui_server --host 0.0.0.0 --port $(PORT) & \
		GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
		GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
		VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
		GEMINI_MODEL=$(GEMINI_MODEL) \
		GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
		GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
		GOOGLE_GENAI_USE_VERTEXAI=$(GOOGLE_GENAI_USE_VERTEXAI) \
		VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
		USE_CLOUD_RAG=$(USE_CLOUD_RAG) \
		USE_LIVE_MCP=$(USE_LIVE_MCP) \
		MCP_AUTHENTICATED_EMPLOYEE_ID=$(MCP_AUTHENTICATED_EMPLOYEE_ID) \
		$(PYTHON) -m google.adk.cli web . --host 0.0.0.0 --port $(ADK_PORT) & \
		wait'

dev: start-local ## Alias for 'make start-local'

local-up: start-local ## Alias for 'make start-local'

test-local: ## Test the local UI + ADK Agent + Live MCP Server (EMP-836) + GCP Vertex AI RAG as a user before deploying to GCP
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
	VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
	GEMINI_MODEL=$(GEMINI_MODEL) \
	GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
	GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
	GOOGLE_GENAI_USE_VERTEXAI=$(GOOGLE_GENAI_USE_VERTEXAI) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=true \
	USE_LIVE_MCP=true \
	MCP_AUTHENTICATED_EMPLOYEE_ID=$(MCP_AUTHENTICATED_EMPLOYEE_ID) \
	$(PYTHON) -m app.ui.local_user_test

local-user-test: test-local ## Alias for 'make test-local'

test: ## Run the unit, integration, E2E, and golden evaluation test suite
	$(PYTHON) -m unittest discover -s tests -v

mcp-unit-test: ## Run deterministic MCP Client & ACL Proxy unit tests
	$(PYTHON) -m unittest tests/unit/test_mcp_client.py -v

mcp-integration-test: ## Run live WorkWeek & ServiceImmediately MCP server integration tests
	USE_LIVE_MCP=true \
	$(PYTHON) -m unittest tests/integration/test_mcp_server_integration.py -v

mcp-e2e-test: ## Run End-to-End ADK Agent -> Live MCP Server + Vertex AI RAG Engine tests
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
	VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
	GEMINI_MODEL=$(GEMINI_MODEL) \
	GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
	GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=true \
	USE_LIVE_MCP=true \
	$(PYTHON) -m unittest tests/integration/test_agent_mcp_e2e.py -v

mcp-test: mcp-unit-test mcp-integration-test mcp-e2e-test ## Run all MCP unit, live integration, and E2E agent tests

# ------------------------------------------------------------------------------
# 2. RAG Policy Search: Ingestion, Retrieval Testing & GCP RAG Provisioning
# ------------------------------------------------------------------------------
rag-ingest: ## Run layout-aware handbook ingestion (C-1..C-6) and write JSONL chunks
	$(PYTHON) -m app.rag.cli ingest \
		--output-dir $(RAG_OUTPUT_DIR) \
		--project-id $(PROJECT_ID)

rag-test: ## Run 7-point RAG retrieval benchmark or custom query (make rag-test QUERY="...")
ifdef QUERY
	$(PYTHON) -m app.rag.cli test-retrieval --query "$(QUERY)"
else
	$(PYTHON) -m app.rag.cli test-retrieval
endif

test-retrieval: rag-test ## Alias for 'make rag-test'

rag-agent-test: ## Run End-to-End ADK Agent -> Vertex AI RAG Engine integration tests
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
	VERTEX_RAG_LOCATION=$(VERTEX_RAG_LOCATION) \
	GEMINI_MODEL=$(GEMINI_MODEL) \
	GEMINI_PRO_MODEL=$(GEMINI_PRO_MODEL) \
	GEMINI_FLASH_MODEL=$(GEMINI_FLASH_MODEL) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=true \
	$(PYTHON) -m unittest tests/integration/test_agent_vertex_rag_e2e.py -v

rag-provision: gcp-enable-apis ## Provision GCS + Vertex AI RAG Engine in GCP, upload chunks & import
	@echo ">>> [1/2] Running C-1..C-6 handbook ingestion & provisioning Vertex AI RAG Engine in $(PROJECT_ID)..."
	$(PYTHON) -m app.rag.cli ingest \
		--output-dir $(RAG_OUTPUT_DIR) \
		--project-id $(PROJECT_ID) \
		--region $(REGION) \
		--upload-gcs $(RAG_BUCKET_URI) \
		--import-discovery-engine \
		--data-store-id $(RAG_DATASTORE_ID)
	@echo ">>> [2/2] Verifying policy retrieval benchmark..."
	$(PYTHON) -m app.rag.cli test-retrieval

# ------------------------------------------------------------------------------
# 3. GCP Project Setup & Container Build (ai-training-van-01)
# ------------------------------------------------------------------------------
gcp-auth-check: ## Configure gcloud project to ai-training-van-01 and verify active account
	gcloud config set project $(PROJECT_ID)
	gcloud auth list --filter=status:ACTIVE

gcp-enable-apis: gcp-auth-check ## Enable required GCP APIs in ai-training-van-01
	gcloud services enable \
		compute.googleapis.com \
		run.googleapis.com \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
		secretmanager.googleapis.com \
		firestore.googleapis.com \
		bigquery.googleapis.com \
		aiplatform.googleapis.com \
		storage.googleapis.com \
		discoveryengine.googleapis.com \
		--project=$(PROJECT_ID)

gcp-setup-ar: gcp-enable-apis ## Ensure Artifact Registry repository 'hr-agent' exists in asia-southeast1
	gcloud artifacts repositories describe $(AR_REPO) \
		--project=$(PROJECT_ID) \
		--location=$(REGION) >/dev/null 2>&1 || \
	gcloud artifacts repositories create $(AR_REPO) \
		--project=$(PROJECT_ID) \
		--location=$(REGION) \
		--repository-format=docker \
		--description="Container repository for Altostrat HR Agent (ACL/PDP & Chat UI BFF)"

cloud-build: gcp-setup-ar ## Build and push acl-pdp and chat-ui-bff container images via Cloud Build
	gcloud builds submit . \
		--project=$(PROJECT_ID) \
		--tag=$(AR_IMAGE_PREFIX)/acl-pdp:$(IMAGE_TAG)
	gcloud artifacts docker tags add \
		$(AR_IMAGE_PREFIX)/acl-pdp:$(IMAGE_TAG) \
		$(AR_IMAGE_PREFIX)/chat-ui-bff:$(IMAGE_TAG) \
		--project=$(PROJECT_ID)

docker-build: ## Build container images locally with Docker and push to Artifact Registry
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet
	docker build -t $(AR_IMAGE_PREFIX)/acl-pdp:$(IMAGE_TAG) \
	             -t $(AR_IMAGE_PREFIX)/chat-ui-bff:$(IMAGE_TAG) .
	docker push $(AR_IMAGE_PREFIX)/acl-pdp:$(IMAGE_TAG)
	docker push $(AR_IMAGE_PREFIX)/chat-ui-bff:$(IMAGE_TAG)

# ------------------------------------------------------------------------------
# 4. Terraform Infrastructure Provisioning (ai-training-van-01)
# ------------------------------------------------------------------------------
tf-init: ## Initialize Terraform in deployment/terraform/single-project
	terraform -chdir=$(TF_DIR) init

tf-validate: tf-init ## Validate Terraform configuration
	terraform -chdir=$(TF_DIR) validate

tf-plan: tf-init ## Plan Terraform infrastructure changes for ai-training-van-01
	terraform -chdir=$(TF_DIR) plan \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)"

tf-apply: tf-init ## Apply Terraform infrastructure to ai-training-van-01
	terraform -chdir=$(TF_DIR) apply -auto-approve \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)"

tf-destroy: tf-init ## Destroy Terraform-managed GCP resources in ai-training-van-01
	terraform -chdir=$(TF_DIR) destroy \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)"

# ------------------------------------------------------------------------------
# 5. Full End-to-End GCP Provisioning Pipeline
# ------------------------------------------------------------------------------
provision: gcp-enable-apis gcp-setup-ar cloud-build tf-init tf-apply ## Provision all GCP resources, Cloud Run services, and RAG corpus in ai-training-van-01
	@echo ">>> Uploading ingested RAG Policy Corpus to $(RAG_BUCKET_URI) & Discovery Engine..."
	$(PYTHON) -m app.rag.cli ingest \
		--output-dir $(RAG_OUTPUT_DIR) \
		--project-id $(PROJECT_ID) \
		--upload-gcs $(RAG_BUCKET_URI) \
		--import-discovery-engine \
		--data-store-id $(RAG_DATASTORE_ID)
	@echo ">>> Running post-provisioning RAG retrieval verification..."
	$(PYTHON) -m app.rag.cli test-retrieval
	@echo ">>> Provisioning complete for project $(PROJECT_ID)!"
