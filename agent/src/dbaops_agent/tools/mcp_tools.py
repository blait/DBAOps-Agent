"""MCP 도구를 LangChain Tool 로 wrap — swarm/ReAct 에이전트가 호출할 수 있도록.

같은 MCPClient 를 재사용하므로 인증/retry/budget 가드는 그대로 유지된다.
"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.tools import tool

from .mcp_client import MCPClient

_client: MCPClient | None = None


def _get_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


def _truncate(obj: Any, max_chars: int = 8000) -> str:
    """LLM 컨텍스트 폭주 방지용 — JSON 문자열로 직렬화 후 길이 제한."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    if len(s) > max_chars:
        return s[:max_chars] + f"\n... (truncated, total {len(s)} chars)"
    return s


# ───────────────────────── OS / 인프라 ─────────────────────────


@tool
def prometheus_query(promql: str, start: str, end: str, step: str = "30s") -> str:
    """Prometheus 시계열을 가져온다 (단일 타깃 node_exporter).

    Args:
        promql: PromQL 식. instance 라벨 필터는 사용하지 말 것.
        start: RFC3339 시작 시각 (예: 2026-05-17T05:00:00+00:00)
        end:   RFC3339 종료 시각
        step:  step (예: "30s", "1m")
    """
    r = _get_client().call("prometheus-query___prometheus_query",
                           {"promql": promql, "start": start, "end": end, "step": step})
    series = (r or {}).get("series") or []
    return _truncate({"n_points": len(series), "series": series[:200]})


@tool
def cloudwatch_metric(namespace: str, metric: str, dimensions: dict[str, str],
                      start: str, end: str, stat: str = "Average", period: int = 60) -> str:
    """AWS CloudWatch GetMetricData 한 메트릭을 가져온다.

    Args:
        namespace: 예 "AWS/EC2", "AWS/RDS"
        metric:    예 "CPUUtilization", "DatabaseConnections"
        dimensions: 예 {"InstanceId": "i-..."}, {"DBInstanceIdentifier": "..."}
        start/end: RFC3339
        stat:      Average / Sum / Maximum / Minimum
        period:    초 단위 (기본 60)
    """
    r = _get_client().call("cloudwatch-metrics___cloudwatch_get_metric_data", {
        "namespace": namespace, "metric": metric, "dimensions": dimensions or {},
        "start": start, "end": end, "stat": stat, "period": period,
    })
    series = (r or {}).get("series") or []
    return _truncate({"n_points": len(series), "series": series[:200]})


# ───────────────────────── DB ─────────────────────────


@tool
def sql_readonly(engine: str, db_id: str, sql: str) -> str:
    """PostgreSQL 또는 MySQL 에 SELECT/SHOW/DESCRIBE/EXPLAIN 쿼리를 실행한다.

    sqlglot AST gate 로 INSERT/UPDATE/DELETE/MERGE/DDL 등은 거부됩니다. statement_timeout 5s.

    Args:
        engine: "postgres" 또는 "mysql"
        db_id:  RDS 인스턴스/클러스터 식별자 (예 "dbaops-poc-aurora-pg", "dbaops-poc-mysql")
        sql:    SELECT / SHOW / DESCRIBE / EXPLAIN [ANALYZE|...] SELECT...
    """
    r = _get_client().call("sql-readonly___sql_readonly",
                           {"engine": engine, "db_id": db_id, "sql": sql})
    rows = (r or {}).get("rows") or []
    cols = (r or {}).get("columns") or []
    return _truncate({"row_count": len(rows), "columns": cols, "rows": rows[:50]})


@tool
def explain_query(engine: str, db_id: str, sql: str, analyze: bool = False) -> str:
    """SQL 의 실행계획을 가져온다 (EXPLAIN [ANALYZE]).

    PG:    EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) <SELECT> 또는 EXPLAIN <SELECT>.
    MySQL: EXPLAIN ANALYZE <SELECT> (8.0.18+) 또는 EXPLAIN FORMAT=TREE <SELECT>.

    Args:
        engine:  "postgres" 또는 "mysql"
        db_id:   RDS 인스턴스/클러스터 식별자
        sql:     실행계획을 보고 싶은 SELECT. EXPLAIN 접두는 자동.
        analyze: True 면 ANALYZE — 실제로 실행하므로 무거운 쿼리에는 주의.
    """
    base = sql.strip().rstrip(";")
    upper = base.upper().lstrip()
    if upper.startswith("EXPLAIN"):
        wrapped = base
    elif engine == "postgres":
        wrapped = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {base}" if analyze else f"EXPLAIN {base}"
    else:
        wrapped = f"EXPLAIN ANALYZE {base}" if analyze else f"EXPLAIN FORMAT=TREE {base}"

    r = _get_client().call("sql-readonly___sql_readonly",
                           {"engine": engine, "db_id": db_id, "sql": wrapped})
    rows = (r or {}).get("rows") or []
    cols = (r or {}).get("columns") or []
    err = (r or {}).get("error")
    if err:
        return _truncate({"error": err, "validated_sql": (r or {}).get("validated_sql")})
    if cols and rows and len(cols) == 1:
        plan_text = "\n".join(str(row[0]) for row in rows)
        return _truncate({"plan": plan_text, "row_count": len(rows)}, max_chars=12000)
    return _truncate({"row_count": len(rows), "columns": cols, "rows": rows[:200]}, max_chars=12000)


@tool
def rds_performance_insights(db_id: str, start: str, end: str,
                             group_by: str = "db.sql_tokenized.statement") -> str:
    """RDS Performance Insights 의 top SQL by AAS 를 가져온다.

    Args:
        db_id:   RDS dbi-resource-id (예 db-XXXXXX...)
        start/end: RFC3339
        group_by: 기본 "db.sql_tokenized.statement"
    """
    r = _get_client().call("rds-pi___rds_performance_insights",
                           {"db_id": db_id, "start": start, "end": end, "group_by": group_by})
    return _truncate(r or {})


@tool
def msk_metric(cluster_arn: str, metric: str, start: str, end: str,
               stat: str = "Average") -> str:
    """MSK CloudWatch 메트릭을 가져온다 (BytesInPerSec, UnderReplicatedPartitions, ConsumerLag 등).

    Args:
        cluster_arn: MSK cluster ARN (없으면 "msk-cluster" placeholder)
        metric:      AWS/Kafka 메트릭명
        start/end:   RFC3339
        stat:        Average / Sum
    """
    r = _get_client().call("msk-metrics___msk_metrics", {
        "cluster_arn": cluster_arn, "metric": metric, "start": start, "end": end, "stat": stat,
    })
    series = (r or {}).get("series") or []
    return _truncate({"n_points": len(series), "series": series[:200]})


# ───────────────────────── Log ─────────────────────────


@tool
def s3_log_fetch(bucket: str, key: str, regex: str | None = None,
                 max_lines: int = 2000) -> str:
    """S3 의 gzip 로그 객체에서 정규식 매치 라인을 가져온다.

    Args:
        bucket: S3 버킷명
        key:    객체 키 (.gz / .log / .txt). 디렉토리 prefix 가 아닌 단일 객체 키여야 한다.
        regex:  적용할 정규식 (None 이면 모든 라인)
        max_lines: 반환할 최대 라인 수
    """
    r = _get_client().call("s3-log-fetch___s3_log_fetch", {
        "bucket": bucket, "key": key, "regex": regex, "max_lines": max_lines,
    })
    lines = (r or {}).get("lines") or []
    return _truncate({"line_count": len(lines), "truncated": (r or {}).get("truncated", False),
                      "lines": lines[:max_lines]})


# ───────────────────────── 그룹 헬퍼 ─────────────────────────


OS_TOOLS = [prometheus_query, cloudwatch_metric]
DB_TOOLS = [sql_readonly, rds_performance_insights, msk_metric, cloudwatch_metric]
LOG_TOOLS = [s3_log_fetch]
QUERY_TOOLS = [explain_query, sql_readonly]


def infra_context() -> dict[str, str]:
    """Runtime env 에서 인프라 식별자(prom instance id, aurora writer id 등) 추출."""
    return {
        "prom_instance_id":  os.environ.get("INFRA_PROM_INSTANCE_ID", ""),
        "aurora_cluster_id": os.environ.get("INFRA_AURORA_CLUSTER_ID", "dbaops-poc-aurora-pg"),
        "aurora_writer_id":  os.environ.get("INFRA_AURORA_WRITER_ID", "dbaops-poc-aurora-pg-writer"),
        "aurora_reader_id":  os.environ.get("INFRA_AURORA_READER_ID", "dbaops-poc-aurora-pg-reader"),
        "mysql_db_id":       os.environ.get("INFRA_MYSQL_DB_ID", "dbaops-poc-mysql"),
        "msk_cluster_name":  os.environ.get("INFRA_MSK_CLUSTER_NAME", "dbaops-poc"),
        "log_bucket":        os.environ.get("INFRA_LOG_BUCKET", ""),
    }
