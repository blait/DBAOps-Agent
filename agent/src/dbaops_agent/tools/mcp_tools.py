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
                             group_by: str = "db.sql_tokenized") -> str:
    """RDS Performance Insights 의 top SQL by AAS 를 가져온다.

    Args:
        db_id:   RDS dbi-resource-id (예 db-XXXXXX...)
        start/end: RFC3339
        group_by: PI group prefix (예 'db.sql_tokenized', 'db.wait_event', 'db.host', 'db.user').
                  dimension full name 도 허용 — Lambda 에서 prefix 로 잘라낸다.
    """
    r = _get_client().call("rds-pi___rds_performance_insights",
                           {"db_id": db_id, "start": start, "end": end, "group_by": group_by})
    return _truncate(r or {})


@tool
def msk_metric(cluster_arn: str, metric: str, start: str, end: str,
               stat: str = "Average", topic: str | None = None,
               consumer_group: str | None = None,
               period: int = 60) -> str:
    """MSK (AWS/Kafka) CloudWatch 메트릭 조회.

    중요 — 메트릭별로 필요한 dimension 이 다르다 (handler 가 자동 구성):
      - BytesInPerSec / BytesOutPerSec / MessagesInPerSec → Cluster Name + Topic 필수
      - MaxOffsetLag / SumOffsetLag / EstimatedMaxTimeLag → Cluster Name + Consumer Group + Topic 필수
      - UnderReplicatedPartitions / GlobalPartitionCount → Cluster Name (broker level)

    topic / consumer_group 인자를 명시하지 않으면 default (dbaops.orders / dbaops-paused) 사용.
    series 가 비어 있으면 (1) 시간 윈도 안에 트래픽 없음, 또는 (2) 잘못된 topic/consumer_group.

    Args:
        cluster_arn:    MSK cluster ARN. "msk-cluster" placeholder 도 OK.
        metric:         AWS/Kafka 메트릭명
        start/end:      RFC3339
        stat:           Average / Sum / Maximum / Minimum
        topic:          예 'dbaops.orders'. BytesIn/Out/MessagesIn/Lag 류 모두에 권장.
        consumer_group: 예 'dbaops-paused'. Lag 류 메트릭에 권장.
        period:         초 단위 (기본 60)
    """
    args: dict[str, Any] = {
        "cluster_arn": cluster_arn, "metric": metric,
        "start": start, "end": end, "stat": stat, "period": period,
    }
    if topic:
        args["topic"] = topic
    if consumer_group:
        args["consumer_group"] = consumer_group
    r = _get_client().call("msk-metrics___msk_metrics", args)
    series = (r or {}).get("series") or []
    return _truncate({
        "n_points":   len(series),
        "metric":     (r or {}).get("metric") or metric,
        "stat":       (r or {}).get("stat") or stat,
        "dimensions": (r or {}).get("dimensions") or [],
        "series":     series[:200],
    })


# ───────────────────────── Log ─────────────────────────


@tool
def s3_list_logs(bucket: str, prefix: str, since_minutes: int = 60,
                 max_keys: int = 50) -> str:
    """S3 prefix 아래 로그 객체 목록 — 어떤 key 가 존재하는지 먼저 탐색.

    log specialist 가 s3_log_fetch 를 호출하기 전에 이 도구로 객체 목록을
    먼저 받아야 한다 (key 를 추측해 호출하면 NoSuchKey 로 실패).

    Args:
        bucket: S3 버킷명 (infra log_bucket)
        prefix: 예 'logs-burst/postgres/' (디렉토리), 'logs/mysql/' 등
        since_minutes: 최근 N 분 안에 last_modified 된 것만 (기본 60)
        max_keys: 반환 객체 수 한도 (기본 50, 최대 1000)
    """
    r = _get_client().call("s3-log-fetch___s3_list_logs", {
        "bucket": bucket, "prefix": prefix,
        "since_minutes": since_minutes, "max_keys": max_keys,
    })
    objs = (r or {}).get("objects") or []
    return _truncate({
        "count": (r or {}).get("count", len(objs)),
        "is_truncated": (r or {}).get("is_truncated", False),
        "objects": objs[:max_keys],
    })


@tool
def s3_log_fetch(bucket: str, key: str, regex: str | None = None,
                 max_lines: int = 2000) -> str:
    """S3 의 gzip 로그 객체에서 정규식 매치 라인을 가져온다.

    Args:
        bucket: S3 버킷명
        key:    객체 키 (.gz / .log / .txt). 디렉토리 prefix 가 아닌 단일 객체 키여야 한다.
                키 모르면 먼저 `s3_list_logs(prefix=...)` 로 목록 조회.
        regex:  적용할 정규식 (None 이면 모든 라인)
        max_lines: 반환할 최대 라인 수
    """
    r = _get_client().call("s3-log-fetch___s3_log_fetch", {
        "bucket": bucket, "key": key, "regex": regex, "max_lines": max_lines,
    })
    lines = (r or {}).get("lines") or []
    return _truncate({"line_count": len(lines), "truncated": (r or {}).get("truncated", False),
                      "lines": lines[:max_lines]})


# ───────────────────────── AWS 인프라 (read-only) ─────────────────────────


def _aws_call(tool_name: str, args: dict) -> dict:
    """aws-api Lambda 의 dispatch wrapper — handler.py 의 _TOOLS 키와 일치."""
    payload = {"tool_name": tool_name, "arguments": args}
    return _get_client().call(f"aws-api___{tool_name}", payload) or {}


@tool
def aws_describe_rds_instances(db_instance_identifier: str | None = None,
                                max_records: int = 50) -> str:
    """AWS RDS DescribeDBInstances — 인스턴스 메타정보(엔진/버전/엔드포인트/PI/Multi-AZ 등) 조회.

    Args:
        db_instance_identifier: 특정 인스턴스 id(비우면 전체)
        max_records: 20~100
    """
    args: dict[str, Any] = {"max_records": max_records}
    if db_instance_identifier:
        args["db_instance_identifier"] = db_instance_identifier
    return _truncate(_aws_call("describe_rds_instances", args))


@tool
def aws_describe_rds_clusters(db_cluster_identifier: str | None = None) -> str:
    """AWS RDS DescribeDBClusters — Aurora 클러스터/멤버(쓰기/리더) 조회.

    Args:
        db_cluster_identifier: 특정 클러스터 id(비우면 전체)
    """
    args: dict[str, Any] = {}
    if db_cluster_identifier:
        args["db_cluster_identifier"] = db_cluster_identifier
    return _truncate(_aws_call("describe_rds_clusters", args))


@tool
def aws_describe_db_log_files(db_instance_identifier: str,
                               filename_contains: str | None = None) -> str:
    """RDS 인스턴스의 DB 엔진 로그 파일 목록(파일명/크기/마지막 갱신).

    Args:
        db_instance_identifier: RDS DBInstanceIdentifier
        filename_contains: 예 'error', 'slowquery', 'audit', 'postgresql.log'
    """
    args: dict[str, Any] = {"db_instance_identifier": db_instance_identifier}
    if filename_contains:
        args["filename_contains"] = filename_contains
    return _truncate(_aws_call("describe_db_log_files", args))


@tool
def aws_download_db_log_file_portion(db_instance_identifier: str, log_file_name: str,
                                      lines: int = 200, regex: str | None = None,
                                      marker: str | None = None) -> str:
    """RDS DB 엔진 로그 파일의 마지막 N 라인을 가져온다(MySQL slow/error, PG postgresql.log).

    marker 없이 호출하면 Lambda 가 자동으로 끝까지 페이지를 돌며 누적 (최대 50 페이지).
    regex 가 주어지면 매칭 라인만 필터링해 lines 줄 반환.

    Args:
        db_instance_identifier: RDS DBInstanceIdentifier
        log_file_name: describe_db_log_files 결과의 log_filename
        lines: 마지막 N 라인 (기본 200, 최대 1000)
        regex: 적용할 정규식 (예: 'still waiting|deadlock', 'Query Text:|duration:')
        marker: 이어 받을 marker(생략 시 끝까지 자동 페이징)
    """
    args: dict[str, Any] = {
        "db_instance_identifier": db_instance_identifier,
        "log_file_name": log_file_name,
        "lines": lines,
    }
    if regex:
        args["regex"] = regex
    if marker:
        args["marker"] = marker
    return _truncate(_aws_call("download_db_log_file_portion", args), max_chars=14000)


@tool
def aws_list_msk_clusters() -> str:
    """AWS MSK ListClustersV2 — Provisioned/Serverless 클러스터 목록."""
    return _truncate(_aws_call("list_msk_clusters", {}))


@tool
def aws_describe_ec2_instances(instance_ids: list[str] | None = None,
                                tag_name_contains: str | None = None,
                                max: int = 50) -> str:
    """AWS EC2 DescribeInstances — EC2 인스턴스 상태/타입/AZ/Name 태그 조회.

    Args:
        instance_ids: i-xxxx... 목록
        tag_name_contains: Name 태그 포함어 검색
        max: 기본 50, 최대 100
    """
    args: dict[str, Any] = {"max": max}
    if instance_ids:
        args["instance_ids"] = instance_ids
    if tag_name_contains:
        args["tag_name_contains"] = tag_name_contains
    return _truncate(_aws_call("describe_ec2_instances", args))


@tool
def aws_list_cloudwatch_alarms(state_value: str | None = None,
                                alarm_name_prefix: str | None = None,
                                max: int = 50) -> str:
    """AWS CloudWatch DescribeAlarms — 메트릭 알람 목록(상태/임계값/마지막 변경).

    Args:
        state_value: OK / ALARM / INSUFFICIENT_DATA
        alarm_name_prefix: 알람 이름 prefix
        max: 기본 50, 최대 100
    """
    args: dict[str, Any] = {"max": max}
    if state_value:
        args["state_value"] = state_value
    if alarm_name_prefix:
        args["alarm_name_prefix"] = alarm_name_prefix
    return _truncate(_aws_call("list_cloudwatch_alarms", args))


@tool
def aws_list_metric_namespaces(namespace: str | None = None,
                                metric_prefix: str | None = None) -> str:
    """AWS CloudWatch ListMetrics — 계정/리전의 namespace + 메트릭 빠른 탐색(존재 여부 확인용).

    Args:
        namespace: 예 AWS/RDS, AWS/EC2
        metric_prefix: 메트릭 이름 prefix
    """
    args: dict[str, Any] = {}
    if namespace:
        args["namespace"] = namespace
    if metric_prefix:
        args["metric_prefix"] = metric_prefix
    return _truncate(_aws_call("list_metric_namespaces", args))


@tool
def aws_describe_pi_dimensions(dbi_resource_id: str, metric: str = "db.load.avg",
                                group_by: str = "db.sql_tokenized",
                                start: str | None = None, end: str | None = None) -> str:
    """RDS PI DescribeDimensionKeys — 그룹별 top dimension(SQL/wait_event 등) 조회.

    Args:
        dbi_resource_id: RDS DbiResourceId(예 db-XXXXXXXX)
        metric: 예 db.load.avg
        group_by: PI group prefix — 'db.sql_tokenized' / 'db.wait_event' / 'db.host' / 'db.user'.
                  dimension full name 도 허용 (Lambda 에서 prefix 로 정규화).
        start/end: RFC3339 (생략 시 최근 1시간)
    """
    args: dict[str, Any] = {
        "dbi_resource_id": dbi_resource_id,
        "metric": metric,
        "group_by": group_by,
    }
    if start:
        args["start"] = start
    if end:
        args["end"] = end
    return _truncate(_aws_call("describe_pi_dimensions", args))


# ───────────────────────── 그룹 헬퍼 ─────────────────────────


AWS_TOOLS = [
    aws_describe_rds_instances,
    aws_describe_rds_clusters,
    aws_describe_db_log_files,
    aws_download_db_log_file_portion,
    aws_list_msk_clusters,
    aws_describe_ec2_instances,
    aws_list_cloudwatch_alarms,
    aws_list_metric_namespaces,
    aws_describe_pi_dimensions,
]

OS_TOOLS = [prometheus_query, cloudwatch_metric]
DB_TOOLS = [sql_readonly, rds_performance_insights, msk_metric, cloudwatch_metric]
LOG_TOOLS = [s3_list_logs, s3_log_fetch, aws_describe_db_log_files, aws_download_db_log_file_portion]
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
