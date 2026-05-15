# syntax=docker/dockerfile:1.6
# AgentCore Runtime 컨테이너 — ARM64
FROM --platform=linux/arm64 python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

# AgentCore SDK harness entrypoint
CMD ["python", "-m", "dbaops_agent.runtime_entry"]
