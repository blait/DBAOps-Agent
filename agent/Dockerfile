# syntax=docker/dockerfile:1.6
# AgentCore Runtime 컨테이너 — ARM64
FROM --platform=linux/arm64 python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

EXPOSE 8080

# AgentCore Runtime 은 :8080/ping, :8080/invocations 를 호출한다.
CMD ["python", "-m", "dbaops_agent.runtime_entry"]
