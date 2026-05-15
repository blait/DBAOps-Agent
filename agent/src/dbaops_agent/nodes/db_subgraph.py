"""DB 서브그래프 — plan(LLM) → fetch_(pg|mysql|kafka)(MCP, 병렬) → correlate → summarize(LLM)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..analyzers.correlate import bucketize, cross_source
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
You are a DBA analyzing PostgreSQL, MySQL, and Kafka health.
Output ONLY a JSON object:
{
  "pg":    {"enable": bool, "queries": [{"name": str, "sql": str}, ...]},
  "mysql": {"enable": bool, "queries": [{"name": str, "sql": str}, ...]},
  "kafka": {"enable": bool, "metrics": [{"name": str, "metric": str}, ...]}
}
Rules:
- SQL must be SELECT-only and reference system catalogs (pg_stat_*, INFORMATION_SCHEMA, performance_schema, INNODB_*).
- Kafka metrics use AWS/Kafka CloudWatch metric names (BytesInPerSec, BytesOutPerSec, UnderReplicatedPartitions, ConsumerLag).
- Skip a section by setting enable=false.
- No prose, no code fences.
"""

_SUMMARY_SYSTEM = """\
You write concise DB performance findings in Korean. Input is a JSON object with
{"pg": [...], "mysql": [...], "kafka": [...], "correlations": [...]}.
Output ONLY a JSON array: [{"title": str, "severity": "info"|"warn"|"error", "evidence": [...]}, ...].
Cite specific objects (rows, statements, metric names) and timestamps when possible.
No prose, no code fences.
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


def _plan(state: AnalysisState) -> dict[str, Any]:
    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}"
    )
    default = {
        "pg":    {"enable": True, "queries": _DEFAULT_PG_QUERIES},
        "mysql": {"enable": True, "queries": _DEFAULT_MYSQL_QUERIES},
        "kafka": {"enable": True, "metrics": _DEFAULT_KAFKA_METRICS},
    }
    obj = llm_json(_PLAN_SYSTEM, user, default=default)
    if not isinstance(obj, dict):
        return default
    return obj


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
                "sql_readonly",
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
                "sql_readonly",
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
                "msk_metrics",
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


def _summarize(payload: dict[str, Any]) -> list[Finding]:
    fallback = []
    for src in ("pg", "mysql"):
        for r in payload.get(src) or []:
            if r.get("rows"):
                fallback.append(
                    {
                        "title": f"{src} {r['name']}: {len(r['rows'])} rows",
                        "severity": "info",
                        "evidence": r["rows"][:5],
                    }
                )
    for r in payload.get("kafka") or []:
        if r.get("series"):
            fallback.append(
                {
                    "title": f"kafka {r['name']}: {len(r['series'])} points",
                    "severity": "info",
                    "evidence": r["series"][:5],
                }
            )
    if payload.get("correlations"):
        fallback.append(
            {
                "title": f"cross-source bursts: {len(payload['correlations'])} buckets",
                "severity": "warn",
                "evidence": payload["correlations"][:5],
            }
        )

    items = llm_json(_SUMMARY_SYSTEM, str(payload), default=fallback) or fallback
    if not isinstance(items, list):
        items = fallback

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
    return findings


def run(state: AnalysisState) -> AnalysisState:
    plan = _plan(state)
    pg = _fetch_pg(state, plan.get("pg") or {})
    mysql = _fetch_mysql(state, plan.get("mysql") or {})
    kafka = _fetch_kafka(state, plan.get("kafka") or {})
    correlations = _correlate(pg, mysql, kafka)
    payload = {"pg": pg, "mysql": mysql, "kafka": kafka, "correlations": correlations}
    findings = _summarize(payload)
    return {"db_findings": findings}
