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


# ───────────────────────── 단일 task 진행 추적 ─────────────────────────


def describe_task(task_id: str) -> dict[str, Any] | None:
    """단일 task 상태 + 컨테이너 + 로그 stream 정보."""
    ecs = _ecs()
    arn = task_id if task_id.startswith("arn:") else f"arn:aws:ecs:{REGION}:{_account_id()}:task/{CLUSTER}/{task_id}"
    desc = ecs.describe_tasks(cluster=CLUSTER, tasks=[arn]).get("tasks") or []
    if not desc:
        return None
    t = desc[0]
    family = (t.get("taskDefinitionArn") or "").rsplit("/", 1)[-1]
    container = (t.get("containers") or [{}])[0]

    # CW Logs stream 정보 추출 — task definition 의 logConfiguration 에서
    log_group = log_stream = None
    try:
        td = ecs.describe_task_definition(taskDefinition=family).get("taskDefinition") or {}
        for c in td.get("containerDefinitions") or []:
            if c.get("name") == container.get("name"):
                lc = (c.get("logConfiguration") or {}).get("options") or {}
                log_group = lc.get("awslogs-group")
                prefix = lc.get("awslogs-stream-prefix")
                if log_group and prefix and container.get("name"):
                    # awslogs stream name = "<prefix>/<container_name>/<task_id>"
                    log_stream = f"{prefix}/{container.get('name')}/{(t.get('taskArn') or '').rsplit('/', 1)[-1]}"
                break
    except Exception:  # noqa: BLE001
        pass

    def _iso(v: Any) -> str | None:
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).isoformat(timespec="seconds")
        return str(v) if v else None

    return {
        "task_id":          (t.get("taskArn") or "").rsplit("/", 1)[-1],
        "family":           family,
        "last_status":      t.get("lastStatus"),
        "desired_status":   t.get("desiredStatus"),
        "stop_code":        t.get("stopCode"),
        "stopped_reason":   (t.get("stoppedReason") or "") or None,
        "container_name":   container.get("name"),
        "container_status": container.get("lastStatus"),
        "exit_code":        container.get("exitCode"),
        "exit_reason":      container.get("reason"),
        "created_at":       _iso(t.get("createdAt")),
        "started_at":       _iso(t.get("startedAt")),
        "stopped_at":       _iso(t.get("stoppedAt")),
        "log_group":        log_group,
        "log_stream":       log_stream,
    }


_ACCOUNT_ID: str | None = None


def _account_id() -> str:
    global _ACCOUNT_ID
    if _ACCOUNT_ID is None:
        _ACCOUNT_ID = boto3.client("sts", region_name=REGION).get_caller_identity().get("Account") or ""
    return _ACCOUNT_ID


def stop_task(task_id: str, reason: str = "stopped from streamlit") -> dict[str, Any]:
    arn = task_id if task_id.startswith("arn:") else f"arn:aws:ecs:{REGION}:{_account_id()}:task/{CLUSTER}/{task_id}"
    return _ecs().stop_task(cluster=CLUSTER, task=arn, reason=reason)


# ───────────────────────── CloudWatch Logs tail ─────────────────────────


def tail_log_events(log_group: str, log_stream: str, *, start_from_head: bool = True,
                    next_token: str | None = None, limit: int = 200) -> dict[str, Any]:
    """get_log_events 한 번 호출. 다음 token + events 를 반환.

    nextForwardToken 으로 다음 호출 시 이어서 읽을 수 있다.
    """
    cw = boto3.client("logs", region_name=REGION)
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "logStreamName": log_stream,
        "limit": limit,
        "startFromHead": start_from_head,
    }
    if next_token:
        kwargs["nextToken"] = next_token
        kwargs.pop("startFromHead", None)
    try:
        resp = cw.get_log_events(**kwargs)
    except cw.exceptions.ResourceNotFoundException:
        return {"events": [], "next_token": next_token, "ready": False}

    events = []
    for e in resp.get("events") or []:
        ts = e.get("timestamp")
        events.append({
            "ts": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                  if isinstance(ts, (int, float)) else None,
            "message": e.get("message") or "",
        })
    return {
        "events":     events,
        "next_token": resp.get("nextForwardToken"),
        "ready":      True,
    }
