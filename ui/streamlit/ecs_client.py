"""ECS task 조회/실행 헬퍼 — Streamlit 'Generators' 패널용."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
CLUSTER = os.environ.get("ECS_CLUSTER", "dbaops-poc")

# 사이드바 트리거 버튼 정의 — task definition + 기본 환경
SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "data-baseline",
        "label": "Baseline 트래픽 (60초)",
        "task_def": "dbaops-poc-data-baseline",
        "duration": 60,
        "env": [],
    },
    {
        "key": "data-lock-contention",
        "label": "PG 락 경합 (3분)",
        "task_def": "dbaops-poc-data-lock-contention",
        "duration": 180,
        "env": [],
    },
    {
        "key": "data-slow-query",
        "label": "MySQL 슬로우 쿼리 (2분)",
        "task_def": "dbaops-poc-data-slow-query",
        "duration": 120,
        "env": [],
    },
    {
        "key": "data-connection-spike",
        "label": "PG 연결 스파이크 (90초)",
        "task_def": "dbaops-poc-data-connection-spike",
        "duration": 90,
        "env": [],
    },
    {
        "key": "data-kafka-isr-shrink",
        "label": "Kafka ISR shrink (60초)",
        "task_def": "dbaops-poc-data-kafka-isr-shrink",
        "duration": 60,
        "env": [],
    },
    {
        "key": "log-postgres-burst",
        "label": "PG 에러 로그 burst (3분, 50 line/s)",
        "task_def": "dbaops-poc-log-postgres",
        "duration": 180,
        "env": [
            {"name": "MODE",          "value": "burst"},
            {"name": "LINES_PER_SEC", "value": "50"},
            {"name": "S3_PREFIX",     "value": "logs-burst"},
        ],
    },
    {
        "key": "log-mysql-burst",
        "label": "MySQL 에러 로그 burst (3분, 50 line/s)",
        "task_def": "dbaops-poc-log-mysql",
        "duration": 180,
        "env": [
            {"name": "MODE",          "value": "burst"},
            {"name": "LINES_PER_SEC", "value": "50"},
            {"name": "S3_PREFIX",     "value": "logs-burst"},
        ],
    },
    {
        "key": "log-kafka-burst",
        "label": "Kafka 에러 로그 burst (3분, 50 line/s)",
        "task_def": "dbaops-poc-log-kafka",
        "duration": 180,
        "env": [
            {"name": "MODE",          "value": "burst"},
            {"name": "LINES_PER_SEC", "value": "50"},
            {"name": "S3_PREFIX",     "value": "logs-burst"},
        ],
    },
]


def _ecs():
    return boto3.client("ecs", region_name=REGION)


def list_running_tasks() -> list[dict[str, Any]]:
    ecs = _ecs()
    arns = ecs.list_tasks(cluster=CLUSTER, desiredStatus="RUNNING").get("taskArns") or []
    if not arns:
        return []
    desc = ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks") or []
    out: list[dict[str, Any]] = []
    for t in desc:
        family = (t.get("taskDefinitionArn") or "").rsplit("/", 1)[-1]
        started = t.get("startedAt") or t.get("createdAt")
        if isinstance(started, datetime):
            started_s = started.astimezone(timezone.utc).isoformat(timespec="seconds")
        else:
            started_s = str(started or "")
        out.append({
            "family":        family,
            "task_id":       (t.get("taskArn") or "").rsplit("/", 1)[-1],
            "last_status":   t.get("lastStatus"),
            "container":     (t.get("containers") or [{}])[0].get("lastStatus"),
            "started_at":    started_s,
        })
    return out


def list_recent_stopped(limit: int = 10) -> list[dict[str, Any]]:
    ecs = _ecs()
    arns = ecs.list_tasks(cluster=CLUSTER, desiredStatus="STOPPED").get("taskArns") or []
    arns = arns[:limit]
    if not arns:
        return []
    desc = ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks") or []
    out: list[dict[str, Any]] = []
    for t in desc:
        family = (t.get("taskDefinitionArn") or "").rsplit("/", 1)[-1]
        stopped = t.get("stoppedAt") or t.get("executionStoppedAt")
        out.append({
            "family":         family,
            "task_id":        (t.get("taskArn") or "").rsplit("/", 1)[-1],
            "stop_code":      t.get("stopCode"),
            "stopped_reason": (t.get("stoppedReason") or "")[:80],
            "stopped_at":     stopped.astimezone(timezone.utc).isoformat(timespec="seconds") if isinstance(stopped, datetime) else "",
            "exit_code":      (t.get("containers") or [{}])[0].get("exitCode"),
        })
    return out


def trigger_scenario(key: str, *, subnets: list[str], security_groups: list[str] | None = None) -> dict[str, Any]:
    sc = next((s for s in SCENARIOS if s["key"] == key), None)
    if sc is None:
        raise ValueError(f"unknown scenario {key}")
    env = list(sc["env"])
    env.append({"name": "DURATION_SEC", "value": str(sc["duration"])})

    netcfg: dict[str, Any] = {
        "subnets": subnets,
        "assignPublicIp": "DISABLED",
    }
    if security_groups:
        netcfg["securityGroups"] = security_groups

    container_name = "log-gen" if sc["task_def"].startswith("dbaops-poc-log-") else "data-gen"

    resp = _ecs().run_task(
        cluster=CLUSTER,
        launchType="FARGATE",
        taskDefinition=sc["task_def"],
        networkConfiguration={"awsvpcConfiguration": netcfg},
        overrides={"containerOverrides": [{"name": container_name, "environment": env}]},
    )
    tasks = resp.get("tasks") or []
    failures = resp.get("failures") or []
    if not tasks:
        return {"ok": False, "failures": failures}
    return {
        "ok": True,
        "task_id": (tasks[0].get("taskArn") or "").rsplit("/", 1)[-1],
        "family": sc["task_def"],
    }


def default_subnets() -> list[str]:
    csv = os.environ.get("ECS_SUBNETS", "")
    return [s.strip() for s in csv.split(",") if s.strip()]


def default_security_groups() -> list[str]:
    csv = os.environ.get("ECS_SECURITY_GROUPS", "")
    return [s.strip() for s in csv.split(",") if s.strip()]
