"""OS 서브그래프 — plan(LLM) → fetch(MCP, Prom + CW 병렬) → anomaly(코드) → summarize(LLM)."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from ..analyzers.anomaly import detect
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
You are a Linux/host metrics analyst.
Two sources are available; produce queries from BOTH:

(1) Prometheus + node_exporter (single target "localhost:9100").
   - Do NOT add `instance=~"..."` label filters.
   - Use rate(...[5m]) / avg_over_time(...[5m]).
   - Metrics: node_cpu_seconds_total, node_memory_MemAvailable_bytes, node_memory_MemTotal_bytes,
     node_disk_io_time_seconds_total, node_disk_read_bytes_total, node_disk_written_bytes_total,
     node_network_receive_bytes_total, node_network_transmit_bytes_total, node_load5.

(2) AWS CloudWatch GetMetricData. Useful for instances Prometheus does not scrape:
   - AWS/EC2 (Prometheus EC2 host {prom_instance_id}): CPUUtilization, NetworkIn, NetworkOut, DiskReadOps, DiskWriteOps.
   - AWS/RDS (Aurora cluster instances {aurora_writer_id}, {aurora_reader_id} and MySQL {mysql_db_id}):
     CPUUtilization, DatabaseConnections, ReadIOPS, WriteIOPS, FreeableMemory, ReadLatency, WriteLatency.
   - dimensions key examples: {"InstanceId": "<id>"}, {"DBInstanceIdentifier": "<id>"}, {"DBClusterIdentifier": "<id>"}.
   - period default 60.

Output ONLY a JSON object:
{
  "prom_queries": [{"name": str, "promql": str}, ...],
  "cw_queries":   [{"name": str, "namespace": str, "metric": str, "dimensions": {str: str}, "stat": str?}, ...]
}
3-6 queries per source. No prose, no code fences.
"""

_SUMMARY_SYSTEM = """\
You analyze OS/infra metric anomalies and write concise findings in Korean.
Each summary input item has fields name (the metric name), source ("prom"|"cw"),
n_points, anomalies (with ts/value/z/reason).
Output ONLY a JSON array: [{"title": str, "severity": "info"|"warn"|"error", "evidence": [...]}, ...].
Be specific about timestamps, magnitudes, and metric source. No prose, no code fences.
"""

_DEFAULT_PROM_QUERIES = [
    {"name": "cpu_used",        "promql": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100"},
    {"name": "mem_used_bytes",  "promql": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes"},
    {"name": "disk_io_seconds", "promql": "rate(node_disk_io_time_seconds_total[5m])"},
    {"name": "net_rx_bytes",    "promql": "rate(node_network_receive_bytes_total[5m])"},
]


def _default_cw_queries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prom_id = os.environ.get("INFRA_PROM_INSTANCE_ID", "")
    if prom_id:
        out.append({"name": "ec2_cpu",      "namespace": "AWS/EC2", "metric": "CPUUtilization", "dimensions": {"InstanceId": prom_id}})
        out.append({"name": "ec2_net_in",   "namespace": "AWS/EC2", "metric": "NetworkIn",      "dimensions": {"InstanceId": prom_id}})
    writer = os.environ.get("INFRA_AURORA_WRITER_ID", "")
    if writer:
        out.append({"name": "aurora_writer_cpu",      "namespace": "AWS/RDS", "metric": "CPUUtilization",       "dimensions": {"DBInstanceIdentifier": writer}})
        out.append({"name": "aurora_writer_conn",     "namespace": "AWS/RDS", "metric": "DatabaseConnections",  "dimensions": {"DBInstanceIdentifier": writer}})
        out.append({"name": "aurora_writer_freeable", "namespace": "AWS/RDS", "metric": "FreeableMemory",       "dimensions": {"DBInstanceIdentifier": writer}})
    mysql = os.environ.get("INFRA_MYSQL_DB_ID", "")
    if mysql:
        out.append({"name": "mysql_cpu",  "namespace": "AWS/RDS", "metric": "CPUUtilization",      "dimensions": {"DBInstanceIdentifier": mysql}})
        out.append({"name": "mysql_conn", "namespace": "AWS/RDS", "metric": "DatabaseConnections", "dimensions": {"DBInstanceIdentifier": mysql}})
    return out


def _plan(state: AnalysisState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}\n"
        f"prom_instance_id={os.environ.get('INFRA_PROM_INSTANCE_ID','')}\n"
        f"aurora_writer_id={os.environ.get('INFRA_AURORA_WRITER_ID','')}\n"
        f"aurora_reader_id={os.environ.get('INFRA_AURORA_READER_ID','')}\n"
        f"mysql_db_id={os.environ.get('INFRA_MYSQL_DB_ID','')}"
    )
    default = {"prom_queries": _DEFAULT_PROM_QUERIES, "cw_queries": _default_cw_queries()}
    obj = llm_json(_PLAN_SYSTEM, user, default=default)
    if not isinstance(obj, dict):
        return default["prom_queries"], default["cw_queries"]
    prom_qs = obj.get("prom_queries") or default["prom_queries"]
    cw_qs = obj.get("cw_queries") or default["cw_queries"]
    return prom_qs, cw_qs


def _fetch(
    state: AnalysisState,
    prom_queries: list[dict[str, Any]],
    cw_queries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Returns: {name: {"source": "prom"|"cw", "series": [(ts, value), ...]}}"""
    start, end = time_range(state)
    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()

    out: dict[str, dict[str, Any]] = {}

    for q in prom_queries:
        try:
            r = client.call(
                "prometheus-query___prometheus_query",
                {"promql": q["promql"], "start": start, "end": end, "step": "30s"},
                cache=cache,
                budget=budget,
            )
            series = (r or {}).get("series") or []
            out[q["name"]] = {
                "source": "prom",
                "series": [(p["ts"], float(p["value"])) for p in series if "value" in p],
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("os_fetch prom %s failed: %s", q.get("name"), e)
            out[q["name"]] = {"source": "prom", "series": []}

    for q in cw_queries:
        try:
            r = client.call(
                "cloudwatch-metrics___cloudwatch_get_metric_data",
                {
                    "namespace":  q["namespace"],
                    "metric":     q["metric"],
                    "dimensions": q.get("dimensions") or {},
                    "start":      start,
                    "end":        end,
                    "stat":       q.get("stat", "Average"),
                    "period":     int(q.get("period", 60)),
                },
                cache=cache,
                budget=budget,
            )
            series = (r or {}).get("series") or []
            out[q["name"]] = {
                "source": "cw",
                "series": [(p["ts"], float(p["value"])) for p in series if "value" in p],
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("os_fetch cw %s failed: %s", q.get("name"), e)
            out[q["name"]] = {"source": "cw", "series": []}

    state["tool_budget"] = budget[0]
    return out


def _anomaly_summary(series_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for name, item in series_map.items():
        series = item["series"]
        anomalies = detect(series)
        summary.append(
            {
                "name": name,
                "source": item.get("source", "prom"),
                "n_points": len(series),
                "anomalies": [
                    {"ts": a.ts, "value": a.value, "z": round(a.z, 2), "reason": a.reason}
                    for a in anomalies[:20]
                ],
            }
        )
    return summary


def _summarize(summary: list[dict[str, Any]]) -> list[Finding]:
    if not any(s["anomalies"] for s in summary):
        return []
    fallback = [
        {
            "title": f"[{s['source']}] {s['name']} anomalies={len(s['anomalies'])}",
            "severity": "warn",
            "evidence": s["anomalies"],
        }
        for s in summary
        if s["anomalies"]
    ]
    items = llm_json(_SUMMARY_SYSTEM, str(summary), default=fallback) or fallback
    if not isinstance(items, list):
        items = fallback

    findings: list[Finding] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        findings.append(
            {
                "id": str(uuid.uuid4())[:8],
                "domain": "os",
                "title": it.get("title", "(untitled)"),
                "severity": it.get("severity", "info"),
                "evidence": it.get("evidence") or [],
                "timestamp": utc_iso(0),
            }
        )
    return findings


def run(state: AnalysisState) -> AnalysisState:
    prom_qs, cw_qs = _plan(state)
    series_map = _fetch(state, prom_qs, cw_qs)
    summary = _anomaly_summary(series_map)
    findings = _summarize(summary)
    return {"os_findings": findings}
