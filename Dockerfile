# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/home/incidentlab/.local/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash incidentlab

USER incidentlab
WORKDIR /app

# Install uv in user home
COPY --from=ghcr.io/astral-sh/uv:latest /uv /home/incidentlab/.local/bin/uv

# --- Runtime build stage ---
FROM base AS runtime

COPY --chown=incidentlab:incidentlab pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=incidentlab:incidentlab migrations/ ./migrations/
COPY --chown=incidentlab:incidentlab data/ ./data/
COPY --chown=incidentlab:incidentlab src/ ./src/
RUN uv sync --locked --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "incidentlab.api:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Transient evaluation target for isolated test runs ---
FROM base AS evaluation

COPY --chown=incidentlab:incidentlab pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra dev --no-install-project

COPY --chown=incidentlab:incidentlab migrations/ ./migrations/
COPY --chown=incidentlab:incidentlab data/ ./data/
COPY --chown=incidentlab:incidentlab src/ ./src/
COPY --chown=incidentlab:incidentlab tests/ ./tests/
COPY --chown=incidentlab:incidentlab evals/ ./evals/
RUN uv sync --locked --extra dev

CMD ["uv", "run", "pytest", "-m", "not live_model"]
