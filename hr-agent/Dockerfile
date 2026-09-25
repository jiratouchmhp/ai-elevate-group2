# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# --- Stage 1: build the React chat UI (SDD §3.10) -----------------------------
FROM node:24-slim AS ui
WORKDIR /ui
COPY ./frontend/package.json ./frontend/package-lock.json* ./
# package-lock.json resolves via a corp-only mirror unreachable from Cloud Build; point the
# same tarball paths at the public registry (npm still verifies each `integrity` hash).
RUN sed -i 's#http://airlock-proxy.uplink.goog:999/npm/artifact-foundry-prod/ah-3p-staging-npm/#https://registry.npmjs.org/#g' package-lock.json \
 && npm ci --no-fund --no-audit
COPY ./frontend ./
RUN npm run build

# --- Stage 2: agent + BFF ------------------------------------------------------
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./
# Dependencies only; the app is imported from /code (uvicorn adds its app dir to sys.path).
# uv.lock resolves via a corp-only mirror unreachable from Cloud Build, so install the exact
# locked versions from public PyPI, verified against the lockfile's sha256 hashes.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
 && uv venv /code/.venv \
 && uv pip install --python /code/.venv/bin/python --index-url https://pypi.org/simple \
      --require-hashes -r /tmp/requirements.txt

COPY ./app ./app
# mock_backends/ is NOT shipped: deployed tools reach the real vendor through hr-acl
# (INTEGRATION_MODE=remote, BACKEND_MODE=mcp). The mock stays in the repo for tests/eval.
COPY --from=ui /ui/dist ./frontend/dist

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION} \
    PATH="/code/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HR_RUNTIME_DIR=/tmp/hr-runtime \
    PORT=8080

# Least privilege: the code is read-only to the service user, except the one directory
# ADK's dev UI (/dev-ui/) rewrites at startup (browser/assets/config/runtime-config.json).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && ADK_UI_CONFIG="$(python -c 'import os, google.adk.cli as c; print(os.path.join(os.path.dirname(c.__file__), "browser", "assets", "config"))')" \
 && mkdir -p "$ADK_UI_CONFIG" && chown -R 10001 "$ADK_UI_CONFIG"
USER 10001

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.fast_api_app:app --host 0.0.0.0 --port ${PORT}"]
