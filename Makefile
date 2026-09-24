# ==============================================================================
# Altostrat Singapore — HR Agentic Assistant (MVP 1) Makefile
# Target GCP Project: ai-training-van-01 (Region: asia-southeast1)
# ==============================================================================

export PATH      := /usr/local/google/home/vannick/google-cloud-sdk/bin:$(PATH)

PROJECT_ID       ?= ai-training-van-01
REGION           ?= asia-southeast1
AR_REPO          ?= hr-agent
IMAGE_TAG        ?= 1.0.0
PORT             ?= 8080
ADK_PORT         ?= 8000
VENV             ?= .venv
PYTHON           := $(shell if [ -x "$(VENV)/bin/python" ] && "$(VENV)/bin/python" -c "import yaml" >/dev/null 2>&1; then echo "$(VENV)/bin/python"; else echo "python3"; fi)
PIP              := $(if $(wildcard $(VENV)/bin/pip),$(VENV)/bin/pip,pip3)
TF_DIR           ?= deployment/terraform/single-project
RAG_OUTPUT_DIR   ?= build/rag
RAG_BUCKET_URI   := gs://$(PROJECT_ID)-hr-policy-corpus
RAG_DATASTORE_ID ?= altostrat-sg-policy-handbook-ds
VERTEX_RAG_CORPUS_ID ?= 4611686018427387904
USE_CLOUD_RAG    ?= true
AR_IMAGE_PREFIX  := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)

.DEFAULT_GOAL := help

.PHONY: help install start run start-adk test \
        rag-ingest rag-test test-retrieval rag-agent-test rag-provision \
        gcp-auth-check gcp-enable-apis gcp-setup-ar \
        cloud-build docker-build \
        tf-init tf-validate tf-plan tf-apply tf-destroy \
        provision

help: ## Show available targets and current GCP configuration
	@echo "=============================================================================="
	@echo " Altostrat Singapore — HR Agentic Assistant (MVP 1)"
	@echo " Target GCP Project    : $(PROJECT_ID)"
	@echo " Target Region         : $(REGION)"
	@echo " RAG Corpus Bucket     : $(RAG_BUCKET_URI)"
	@echo " Vertex AI RAG Corpus  : $(VERTEX_RAG_CORPUS_ID) ($(REGION))"
	@echo "=============================================================================="
	@echo ""
	@echo "Application Startup & Local Testing:"
	@echo "  make install          Create .venv and install project + dev dependencies"
	@echo "  make start            Start FastAPI + AG-UI SSE server locally on port $(PORT)"
	@echo "  make start-adk        Start Google ADK Web UI locally on port $(ADK_PORT)"
	@echo "  make test             Run full unit & golden evaluation test suite"
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
# 1. Local Setup, Application Startup & Tests
# ------------------------------------------------------------------------------
install: ## Install Python dependencies into virtual environment
	@if [ ! -x "$(VENV)/bin/pip" ]; then python3 -m venv --clear $(VENV); fi
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"

start: ## Start the FastAPI + AG-UI BFF application server on PORT (default: 8080)
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(REGION) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=$(USE_CLOUD_RAG) \
	$(PYTHON) -m app.ui.ag_ui_server --host 0.0.0.0 --port $(PORT)

run: start ## Alias for 'make start'

start-adk: ## Launch the Google ADK developer Web UI on ADK_PORT (default: 8000)
	GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	GOOGLE_CLOUD_LOCATION=$(REGION) \
	VERTEX_RAG_CORPUS_ID=$(VERTEX_RAG_CORPUS_ID) \
	USE_CLOUD_RAG=$(USE_CLOUD_RAG) \
	$(PYTHON) -m google.adk.cli web . --port $(ADK_PORT)

test: ## Run the unit and golden evaluation test suite
	$(PYTHON) -m unittest discover -s tests -v

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
	GOOGLE_CLOUD_LOCATION=$(REGION) \
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
