"""OS 서브그래프 — plan(LLM) → fetch(MCP) → anomaly(코드) → summarize(LLM)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..analyzers.anomaly import detect
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
You are a Linux/host metrics analyst. Output ONLY a JSON object with key "queries":
[{"name": str, "promql": str}, ...]. 3-6 queries covering CPU, memory, disk I/O, network.
Use rate()/avg_over_time() where appropriate. No prose, no code fences.
"""

_SUMMARY_SYSTEM = """\
You analyze OS metric anomalies and write concise findings in Korean.
Input: list of (query name, anomalies). Output a JSON array of findings:
[{"title": str, "severity": "info"|"warn"|"error", "evidence": [...]}, ...].
Be specific about timestamps and magnitudes. No prose outside JSON, no code fences.
"""

_DEFAULT_QUERIES: list[dict[str, str]] = [
    {"name": "cpu_used", "promql": "100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100"},
    {"name": "mem_used_bytes", "promql": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes"},
    {"name": "disk_io_seconds", "promql": "rate(node_disk_io_time_seconds_total[5m])"},
    {"name": "net_rx_bytes", "promql": "rate(node_network_receive_bytes_total[5m])"},
]


def _offline() -> bool:
    return os.environ.get("DBAOPS_OFFLINE", "").lower() in ("1", "true", "yes")


def _strip_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s


def _utc_iso_minus(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def _plan(state: AnalysisState) -> list[dict[str, str]]:
    if _offline():
        return _DEFAULT_QUERIES
    from ..llm import get_llm  # 지연 import: offline 모드에서는 boto3 인증 시도하지 않음

    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}"
    )
    try:
        resp = get_llm().invoke([SystemMessage(content=_PLAN_SYSTEM), HumanMessage(content=user)])
        text = _strip_fence(getattr(resp, "content", str(resp)))
        plan = json.loads(text)
        return plan.get("queries") or _DEFAULT_QUERIES
    except Exception as e:
        logger.warning("os_plan LLM failed (%s) — falling back to defaults", e)
        return _DEFAULT_QUERIES


def _fetch(state: AnalysisState, queries: list[dict[str, str]]) -> dict[str, list[tuple[str, float]]]:
    req = state.get("request") or {}
    tr = req.get("time_range") or {}
    start = tr.get("start") or _utc_iso_minus(3600)
    end = tr.get("end") or _utc_iso_minus(0)

    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()

    out: dict[str, list[tuple[str, float]]] = {}
    for q in queries:
        try:
            r = client.call(
                "prometheus_query",
                {"promql": q["promql"], "start": start, "end": end, "step": "30s"},
                cache=cache,
                budget=budget,
            )
            series = (r or {}).get("series") or []
            out[q["name"]] = [(p["ts"], float(p["value"])) for p in series if "value" in p]
        except Exception as e:
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
                    {"ts": a.ts, "value": a.value, "z": round(a.z, 2)} for a in anomalies[:20]
                ],
            }
        )
    return summary


def _summarize(summary: list[dict[str, Any]]) -> list[Finding]:
    if not any(s["anomalies"] for s in summary):
        return []
    if _offline():
        items = [
            {
                "title": f"{s['name']} anomalies={len(s['anomalies'])}",
                "severity": "warn",
                "evidence": s["anomalies"],
            }
            for s in summary
            if s["anomalies"]
        ]
    else:
        from ..llm import get_llm

        try:
            resp = get_llm().invoke(
                [
                    SystemMessage(content=_SUMMARY_SYSTEM),
                    HumanMessage(content=json.dumps(summary, ensure_ascii=False)),
                ]
            )
            text = _strip_fence(getattr(resp, "content", str(resp)))
            items = json.loads(text)
        except Exception as e:
            logger.warning("os_summarize LLM failed (%s) — emitting raw findings", e)
            items = [
                {
                    "title": f"{s['name']} anomalies={len(s['anomalies'])}",
                    "severity": "warn",
                    "evidence": s["anomalies"],
                }
                for s in summary
                if s["anomalies"]
            ]
    findings: list[Finding] = []
    for it in items:
        findings.append(
            {
                "id": str(uuid.uuid4())[:8],
                "domain": "os",
                "title": it.get("title", "(untitled)"),
                "severity": it.get("severity", "info"),
                "evidence": it.get("evidence") or [],
                "timestamp": _utc_iso_minus(0),
            }
        )
    return findings


def run(state: AnalysisState) -> AnalysisState:
    queries = _plan(state)
    series_map = _fetch(state, queries)
    summary = _anomaly_summary(series_map)
    findings = _summarize(summary)
    return {"os_findings": findings}
