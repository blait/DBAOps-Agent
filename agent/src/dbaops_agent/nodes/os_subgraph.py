"""OS 서브그래프 — plan(LLM) → fetch(MCP) → anomaly(코드) → summarize(LLM)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..analyzers.anomaly import detect
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
You are a Linux/host metrics analyst writing PromQL for node_exporter on a single Prometheus host.
Constraints:
- Do NOT add `instance=~"..."` label filters. The Prometheus is single-target ("localhost:9100").
- Use `node_cpu_seconds_total`, `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes`,
  `node_disk_io_time_seconds_total`, `node_disk_read_bytes_total`, `node_disk_written_bytes_total`,
  `node_network_receive_bytes_total`, `node_network_transmit_bytes_total`, `node_load5`.
- Use rate(...[5m]) or avg_over_time(...[5m]).
Output ONLY a JSON object: {"queries": [{"name": str, "promql": str}, ...]} (3-6 queries covering CPU, memory, disk I/O, network).
No prose, no code fences.
"""

_SUMMARY_SYSTEM = """\
You analyze OS metric anomalies and write concise findings in Korean.
Output ONLY a JSON array: [{"title": str, "severity": "info"|"warn"|"error", "evidence": [...]}, ...].
Be specific about timestamps and magnitudes. No prose, no code fences.
"""

_DEFAULT_QUERIES = [
    {"name": "cpu_used", "promql": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100"},
    {"name": "mem_used_bytes", "promql": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes"},
    {"name": "disk_io_seconds", "promql": "rate(node_disk_io_time_seconds_total[5m])"},
    {"name": "net_rx_bytes", "promql": "rate(node_network_receive_bytes_total[5m])"},
]


def _plan(state: AnalysisState) -> list[dict[str, str]]:
    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}"
    )
    obj = llm_json(_PLAN_SYSTEM, user, default={"queries": _DEFAULT_QUERIES})
    queries = (obj or {}).get("queries") if isinstance(obj, dict) else None
    return queries or _DEFAULT_QUERIES


def _fetch(state: AnalysisState, queries: list[dict[str, str]]) -> dict[str, list[tuple[str, float]]]:
    start, end = time_range(state)
    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()

    out: dict[str, list[tuple[str, float]]] = {}
    for q in queries:
        try:
            r = client.call(
                "prometheus-query___prometheus_query",
                {"promql": q["promql"], "start": start, "end": end, "step": "30s"},
                cache=cache,
                budget=budget,
            )
            series = (r or {}).get("series") or []
            out[q["name"]] = [(p["ts"], float(p["value"])) for p in series if "value" in p]
        except Exception as e:  # noqa: BLE001
            logger.warning("os_fetch %s failed: %s", q["name"], e)
            out[q["name"]] = []
    state["tool_budget"] = budget[0]
    return out


def _anomaly_summary(series_map: dict[str, list[tuple[str, float]]]) -> list[dict[str, Any]]:
    summary = []
    for name, series in series_map.items():
        anomalies = detect(series)
        summary.append(
            {
                "name": name,
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
            "title": f"{s['name']} anomalies={len(s['anomalies'])}",
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
    queries = _plan(state)
    series_map = _fetch(state, queries)
    summary = _anomaly_summary(series_map)
    findings = _summarize(summary)
    return {"os_findings": findings}
