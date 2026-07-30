# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────────
# dpo-agent web — multi-stage Dockerfile
#
# Build stage: install all dependencies (heavy).
# Runtime stage: copy the installed packages + source code (lightweight).
# Final image is ~500MB instead of ~1.5GB.
# ──────────────────────────────────────────────────────────────────────────────

# ─── Stage 1: build ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS build

WORKDIR /build

# Install build-time dependencies (gcc for pydantic-core, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dpo-agent source + pyproject first (better Docker layer caching:
# deps are only re-installed when pyproject.toml changes).
COPY pyproject.toml README.md ./
COPY dpo_agent ./dpo_agent

# Install dpo-agent with the [server] extra (FastAPI + uvicorn).
RUN pip install --no-cache-dir --prefix=/install ".[server]"

# Copy the kgpipeline from the sibling repo (wiki-contracts) into
# the image, IF it's available at build time. The default path
# is ../wiki-contracts/kgpipeline, but this can be overridden
# via the KGPIPELINE_PATH build arg.
#
# If kgpipeline is not present (e.g. a fresh checkout that hasn't
# been placed next to wiki-contracts), the kg_build task will
# raise a clear ImportError at runtime. The rest of dpo-agent
# works without kgpipeline.
#
# Implementation: instead of `COPY src dst` (which fails if src
# doesn't exist), we use `COPY --from=build_context` via a
# bind-mounted path. The simpler approach is to use a `RUN`
# with a shell conditional:
ARG KGPIPELINE_PATH=../wiki-contracts/kgpipeline
RUN if [ -d "${KGPIPELINE_PATH}" ]; then \
        echo "Vendoring kgpipeline from ${KGPIPELINE_PATH}..." && \
        mkdir -p /usr/local/lib/python3.11/site-packages/kgpipeline && \
        cp -r ${KGPIPELINE_PATH}/. \
              /usr/local/lib/python3.11/site-packages/kgpipeline/ && \
        echo "kgpipeline vendored successfully"; \
    else \
        echo "WARNING: kgpipeline not found at ${KGPIPELINE_PATH}." && \
        echo "         The kg_build task will be unavailable at runtime." && \
        echo "         Set KGPIPELINE_PATH to a valid directory to include it."; \
    fi

# ─── Stage 2: runtime ───────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime deps only (no gcc, no build tools). We need curl for the
# healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for the app. The web service runs as this user.
RUN useradd --create-home --shell /bin/bash dpo

# Copy the install prefix from the build stage.
COPY --from=build /install /usr/local

# Copy the application code.
COPY --chown=dpo:dpo . /app

# Create a directory for SQLite graph databases (kgpipeline output) and
# make it writable by the dpo user.
RUN mkdir -p /app/data && chown dpo:dpo /app/data

# Default environment variables. Override with the docker-compose env
# block or a .env file.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DPO_AGENT_HOST=0.0.0.0 \
    DPO_AGENT_PORT=8000 \
    DPO_AGENT_DATA_DIR=/app/data

# Healthcheck: curl the /healthz endpoint. Curl is installed above.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

USER dpo

EXPOSE 8000

# Run uvicorn with 4 workers. The SSE endpoint works correctly
# across workers because the threading model puts the pipeline
# in a thread inside each request handler.
CMD ["uvicorn", "dpo_agent.examples.fastapi_server:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", \
     "--log-level", "info", \
     "--access-log"]
