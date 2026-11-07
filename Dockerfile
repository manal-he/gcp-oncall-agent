# syntax=docker/dockerfile:1.7
# Multi-stage build: UI assets -> Python venv -> slim non-root runtime.

FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.11-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Optional extras baked into the image (real GCP backend, Gemini/Vertex, OTLP export).
ARG EXTRAS=gcp,gemini,otlp
RUN python -m venv /opt/venv && /opt/venv/bin/pip install ".[${EXTRAS}]"

FROM python:3.11-slim AS runtime
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
COPY --from=ui /ui/dist /app/ui/dist
COPY evals /app/evals
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ONCALL_UI_DIST=/app/ui/dist \
    PORT=8080
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')" || exit 1
# Cloud Run injects PORT; exec so uvicorn receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn oncall_agent.api.app:create_app --factory --host 0.0.0.0 --port ${PORT}"]
