# syntax=docker/dockerfile:1
# Lore Keeper — multi-stage: uv installs deps in builder; runtime is Python slim + venv only.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY requirements.lock .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-cache -r requirements.lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-cache --upgrade \
    --index-url https://download.pytorch.org/whl/cpu torch \
    && uv pip install --python /opt/venv/bin/python --no-cache --upgrade numpy

# --- runtime: no uv, no pip caches, no build-only layers ---
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends curl libopenblas0-pthread libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_MAX_UPLOAD_SIZE=500 \
    HEALTH_PORT=8080 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

# Outside /app so `docker compose` bind-mount `.:/app` cannot overwrite the entrypoint.
# Copy as /entrypoint.sh (standard path). Use `sh` explicitly + strip CR so Windows CRLF
# in the repo cannot cause: exec /entrypoint.sh: no such file or directory
COPY scripts/docker-entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

WORKDIR /app
COPY app.py main.py ingest.py VERSION ./
COPY core ./core
COPY services ./services

EXPOSE 8501 8080

ENTRYPOINT ["/bin/sh", "/entrypoint.sh", "/docker-entrypoint.sh"]
