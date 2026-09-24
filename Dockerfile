FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GOOGLE_CLOUD_PROJECT=ai-training-van-01 \
    GOOGLE_CLOUD_LOCATION=global \
    VERTEX_RAG_LOCATION=asia-southeast1 \
    GEMINI_MODEL=gemini-3.8-flash \
    GEMINI_PRO_MODEL=gemini-3.8-flash \
    GEMINI_FLASH_MODEL=gemini-3.8-flash

COPY pyproject.toml README.md ./
COPY app ./app
COPY docs ./docs

RUN pip install --no-cache-dir . && \
    python3 -m app.rag.cli ingest --output-dir /app/build/rag

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.ui.ag_ui_server:app --host 0.0.0.0 --port ${PORT:-8080}"]
