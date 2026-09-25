# hr-acl — Integration Plane service (Cloud Run). Build from the hr-agent/ root:
#   gcloud builds submit --config deployment/cloudbuild.yaml   (or)
#   docker build -f acl_service/Dockerfile -t hr-acl .
FROM python:3.12-slim

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
COPY ./acl_service ./acl_service
# mock_backends/ is NOT shipped: hr-acl forwards to the real vendor MCP (BACKEND_MODE=mcp).

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION} \
    PATH="/code/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    BACKEND_MODE=mcp \
    LEDGER_BACKEND=firestore \
    AUDIT_SINK=stdout \
    HR_RUNTIME_DIR=/tmp/hr-runtime \
    PORT=8080

# Least privilege: the code is read-only to the service user.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8080
CMD ["sh", "-c", "uvicorn acl_service.main:app --host 0.0.0.0 --port ${PORT}"]
