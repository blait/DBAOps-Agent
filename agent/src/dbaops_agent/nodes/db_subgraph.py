"""DB 서브그래프 — plan(LLM) → fetch_(pg|mysql|kafka)(MCP, 병렬) → correlate → summarize(LLM)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import time

from ..analyzers.correlate import bucketize, cross_source
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, trace, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
당신은 PostgreSQL / MySQL / Kafka 상태를 진단하는 DBA 입니다.
사용자 요청을 보고 어떤 질의/메트릭을 봐야 가설을 검증할 수 있을지 판단합니다.

출력은 JSON 한 객체:
{
  "reasoning": "사용자 요청을 X 로 해석했고, 이를 검증하기 위해 PG 의 A, MySQL 의 B, Kafka 의 C 를 확인하겠다 — 한국어 2~3 문장",
  "pg":    {"enable": bool, "queries": [{"name": str, "sql": str}, ...]},
  "mysql": {"enable": bool, "queries": [{"name": str, "sql": str}, ...]},
  "kafka": {"enable": bool, "metrics": [{"name": str, "metric": str}, ...]}
}

규칙:
- SQL 은 SELECT 전용, system catalog (pg_stat_*, INFORMATION_SCHEMA, performance_schema, INNODB_*) 만.
- Kafka metric 이름은 AWS/Kafka CloudWatch (BytesInPerSec, BytesOutPerSec, UnderReplicatedPartitions, ConsumerLag).
- 필요 없는 섹션은 enable=false.
- JSON 외 prose/코드 펜스 금지.
"""

_SUMMARY_SYSTEM = """\
당신은 DB 성능 분석가입니다. 입력은 PG/MySQL/Kafka 도구 호출 결과와 cross-source correlation 입니다.

[reasoning 작성 규칙]
2~4 문장. **출처 도구명 + 구체적 row/value + 시점**을 반드시 인용:
좋은 예) "sql_readonly(postgres, pg.active_sessions) 결과 8 세션이 wait_event=Lock/tuple 로
        대기 (PID 8729~8731). 동일 시점 msk_metric (MaxOffsetLag) 73k → connection 측 적체 의심."
나쁜 예) "락 경합이 보였다." (도구/수치 없음)

[finding 작성 규칙]
- title 에 도구·수치 명시 — 예: "pg_stat_activity: 8 세션 Lock/tuple wait (active_sessions)"
- evidence 첫 항목은 반드시 다음 dict:
    {"tool": "sql_readonly"|"rds_performance_insights"|"msk_metric"|"cloudwatch_metric",
     "query_or_metric": "<쿼리명/메트릭명>",
     "n_rows": int 또는 "value": <float>,
     "ts": "<RFC3339>",
     "summary": "한 줄 요약 (예: 8 세션이 Lock/tuple wait_event)"}
- evidence 의 두 번째 이후엔 핵심 row/value sample 첨부 가능 (5건 이내).

출력은 JSON 한 객체:
{
  "reasoning": "...",
  "findings": [{"title": str, "severity": "info"|"warn"|"error", "evidence": [...]}, ...]
}
JSON 외 금지.
"""

_DEFAULT_PG_QUERIES = [
    {"name": "active_sessions", "sql": "select pid, state, wait_event_type, wait_event, query from pg_stat_activity where state <> 'idle' limit 50"},
    {"name": "top_statements",  "sql": "select queryid, calls, mean_exec_time, total_exec_time, query from pg_stat_statements order by total_exec_time desc limit 10"},
    {"name": "lock_waits",      "sql": "select * from pg_locks where not granted limit 50"},
]

_DEFAULT_MYSQL_QUERIES = [
    {"name": "top_digests", "sql": "select digest_text, count_star, sum_timer_wait/1e9 as ms from performance_schema.events_statements_summary_by_digest order by sum_timer_wait desc limit 10"},
    {"name": "innodb_lock_waits", "sql": "select * from performance_schema.data_lock_waits limit 50"},
]

_DEFAULT_KAFKA_METRICS = [
    {"name": "bytes_in",  "metric": "BytesInPerSec"},
    {"name": "bytes_out", "metric": "BytesOutPerSec"},
    {"name": "urp",       "metric": "UnderReplicatedPartitions"},
    {"name": "lag",       "metric": "MaxOffsetLag"},
]


def _plan(state: AnalysisState) -> tuple[dict[str, Any], str]:
    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}"
    )
    default = {
        "reasoning": "기본 PG/MySQL/Kafka 진단 쿼리 셋(폴백)을 실행합니다.",
        "pg":    {"enable": True, "queries": _DEFAULT_PG_QUERIES},
        "mysql": {"enable": True, "queries": _DEFAULT_MYSQL_QUERIES},
        "kafka": {"enable": True, "metrics": _DEFAULT_KAFKA_METRICS},
    }
    obj = llm_json(_PLAN_SYSTEM, user, default=default)
    if not isinstance(obj, dict):
        obj = default
    reasoning = obj.get("reasoning") or default["reasoning"]
    return obj, reasoning


def _fetch_pg(state: AnalysisState, plan: dict[str, Any]) -> list[dict]:
    if not plan.get("enable", True):
        return []
    queries = plan.get("queries") or _DEFAULT_PG_QUERIES
    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()
    db_id = (state.get("request") or {}).get("targets") or ["aurora-pg"]
    out: list[dict] = []
    for q in queries:
        try:
            r = client.call(
                "sql-readonly___sql_readonly",
                {"engine": "postgres", "db_id": db_id[0], "sql": q["sql"]},
                cache=cache,
                budget=budget,
            )
            out.append({"name": q["name"], "rows": (r or {}).get("rows") or [], "ts": utc_iso(0)})
        except Exception as e:  # noqa: BLE001
            logger.warning("db_fetch_pg %s failed: %s", q.get("name"), e)
    state["tool_budget"] = budget[0]
    return out


def _fetch_mysql(state: AnalysisState, plan: dict[str, Any]) -> list[dict]:
    if not plan.get("enable", True):
        return []
    queries = plan.get("queries") or _DEFAULT_MYSQL_QUERIES
    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()
    db_id = (state.get("request") or {}).get("targets") or ["mysql"]
    out: list[dict] = []
    for q in queries:
        try:
            r = client.call(
                "sql-readonly___sql_readonly",
                {"engine": "mysql", "db_id": db_id[0], "sql": q["sql"]},
                cache=cache,
                budget=budget,
            )
            out.append({"name": q["name"], "rows": (r or {}).get("rows") or [], "ts": utc_iso(0)})
        except Exception as e:  # noqa: BLE001
            logger.warning("db_fetch_mysql %s failed: %s", q.get("name"), e)
    state["tool_budget"] = budget[0]
    return out


def _fetch_kafka(state: AnalysisState, plan: dict[str, Any]) -> list[dict]:
    if not plan.get("enable", True):
        return []
    metrics = plan.get("metrics") or _DEFAULT_KAFKA_METRICS
    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()
    targets = (state.get("request") or {}).get("targets") or []
    cluster_arn = next((t for t in targets if t.startswith("arn:aws:kafka")), "msk-cluster")
    start, end = time_range(state)
    out: list[dict] = []
    for m in metrics:
        try:
            r = client.call(
                "msk-metrics___msk_metrics",
                {"cluster_arn": cluster_arn, "metric": m["metric"], "start": start, "end": end},
                cache=cache,
                budget=budget,
            )
            series = (r or {}).get("series") or []
            out.append({"name": m["name"], "series": series})
        except Exception as e:  # noqa: BLE001
            logger.warning("db_fetch_kafka %s failed: %s", m.get("name"), e)
    state["tool_budget"] = budget[0]
    return out


def _correlate(pg: list[dict], mysql: list[dict], kafka: list[dict]) -> list[dict]:
    """발화 시점만 모아 60초 윈도로 묶는다."""
    by_source: dict[str, list[dict]] = {"pg": [], "mysql": [], "kafka": []}
    for r in pg:
        if r.get("rows"):
            by_source["pg"].append({"ts": r.get("ts"), "name": r.get("name"), "n_rows": len(r["rows"])})
    for r in mysql:
        if r.get("rows"):
            by_source["mysql"].append({"ts": r.get("ts"), "name": r.get("name"), "n_rows": len(r["rows"])})
    for r in kafka:
        for p in r.get("series") or []:
            ts = p.get("ts")
            v = p.get("value")
            if ts is None or v is None or v == 0:
                continue
            by_source["kafka"].append({"ts": ts, "name": r["name"], "value": v})
    corr = bucketize(by_source, window_sec=60)
    cross = cross_source(corr, min_sources=2)
    return [{"bucket": c.bucket, "sources": list(c.sources.keys())} for c in cross]


def _summarize(payload: dict[str, Any]) -> tuple[list[Finding], str]:
    fb_findings: list[dict] = []
    for src in ("pg", "mysql"):
        for r in payload.get(src) or []:
            if r.get("rows"):
                fb_findings.append({
                    "title": f"{src} {r['name']}: {len(r['rows'])} rows",
                    "severity": "info",
                    "evidence": r["rows"][:5],
                })
    for r in payload.get("kafka") or []:
        if r.get("series"):
            fb_findings.append({
                "title": f"kafka {r['name']}: {len(r['series'])} points",
                "severity": "info",
                "evidence": r["series"][:5],
            })
    if payload.get("correlations"):
        fb_findings.append({
            "title": f"cross-source bursts: {len(payload['correlations'])} buckets",
            "severity": "warn",
            "evidence": payload["correlations"][:5],
        })
    fallback = {
        "reasoning": "LLM 요약 실패로, 도구 결과를 그대로 카운트한 fallback finding 을 생성했습니다.",
        "findings": fb_findings,
    }

    obj = llm_json(_SUMMARY_SYSTEM, str(payload), default=fallback)
    if not isinstance(obj, dict):
        obj = fallback
    items = obj.get("findings") or fb_findings
    reasoning = obj.get("reasoning") or fallback["reasoning"]
    if not isinstance(items, list):
        items = fb_findings

    findings: list[Finding] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        findings.append(
            {
                "id": str(uuid.uuid4())[:8],
                "domain": "db",
                "title": it.get("title", "(untitled)"),
                "severity": it.get("severity", "info"),
                "evidence": it.get("evidence") or [],
                "timestamp": utc_iso(0),
            }
        )
    return findings, reasoning


def run(state: AnalysisState) -> AnalysisState:
    events: list[dict] = [trace("db_subgraph", "enter", phase="enter")]

    t0 = time.time()
    plan, plan_reasoning = _plan(state)
    pg_n = len((plan.get("pg") or {}).get("queries") or [])
    my_n = len((plan.get("mysql") or {}).get("queries") or [])
    kf_n = len((plan.get("kafka") or {}).get("metrics") or [])
    events.append(trace(
        "db.plan",
        f"pg={pg_n} mysql={my_n} kafka={kf_n}",
        phase="thought",
        reasoning=plan_reasoning,
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    pg = _fetch_pg(state, plan.get("pg") or {})
    mysql = _fetch_mysql(state, plan.get("mysql") or {})
    kafka = _fetch_kafka(state, plan.get("kafka") or {})
    pg_rows = sum(len(r.get("rows") or []) for r in pg)
    my_rows = sum(len(r.get("rows") or []) for r in mysql)
    kf_pts = sum(len(r.get("series") or []) for r in kafka)
    fetch_reasoning = (
        f"MCP `sql-readonly___sql_readonly` 로 PG {pg_n} / MySQL {my_n} 쿼리를 실행하고 "
        f"`msk-metrics___msk_metrics` 로 Kafka 메트릭 {kf_n} 건을 호출했습니다. "
        f"수신: PG rows={pg_rows}, MySQL rows={my_rows}, Kafka points={kf_pts}."
    )
    events.append(trace(
        "db.fetch",
        f"pg_rows={pg_rows} mysql_rows={my_rows} kafka_points={kf_pts}",
        phase="thought",
        reasoning=fetch_reasoning,
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    correlations = _correlate(pg, mysql, kafka)
    events.append(trace(
        "db.correlate",
        f"cross_source_buckets={len(correlations)}",
        reasoning=(
            f"60초 윈도 cross-source 발화 버킷 {len(correlations)}개 — "
            "동일 시간대에 두 소스 이상이 같이 비정상이면 인과 가설을 만들 가치가 있다고 판단합니다."
        ),
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    payload = {"pg": pg, "mysql": mysql, "kafka": kafka, "correlations": correlations}
    findings, summarize_reasoning = _summarize(payload)
    events.append(trace(
        "db.summarize",
        f"findings={len(findings)}",
        phase="thought",
        reasoning=summarize_reasoning,
        duration_ms=int((time.time() - t0) * 1000),
    ))

    return {"db_findings": findings, "trace": events}
