"""OS 서브그래프 — plan(LLM) → fetch(MCP, Prom + CW 병렬) → anomaly(코드) → summarize(LLM)."""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from ..analyzers.anomaly import detect
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, trace, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
당신은 호스트/인프라 메트릭 분석가입니다. 사용자 요청과 컨텍스트를 보고 다음을 출력합니다:

(A) 어떤 가설을 가지고 어떤 메트릭을 봐야 하는지를 한국어로 짧게 정리한 reasoning
(B) Prometheus PromQL 쿼리 목록
(C) CloudWatch GetMetricData 쿼리 목록

규칙:
- Prometheus 는 단일 타깃(node_exporter localhost:9100)이라 instance 라벨 필터 금지.
- node_cpu_seconds_total, node_memory_*, node_disk_*, node_network_*, node_load5 사용.
- rate(...[5m]) / avg_over_time(...[5m]) 사용.
- CW namespace 는 AWS/EC2 (ec2-prom 호스트 {prom_instance_id}) 또는 AWS/RDS
  (Aurora writer/reader, MySQL {mysql_db_id}). dimensions 는 {"InstanceId":"..."} / {"DBInstanceIdentifier":"..."}.

출력은 JSON 한 객체만:
{
  "reasoning": "사용자 요청을 보고 X 가 의심되어 PromQL Y 와 CW Z 를 보겠다 — 한국어 2~3 문장",
  "prom_queries": [{"name": str, "promql": str}, ...],
  "cw_queries":   [{"name": str, "namespace": str, "metric": str, "dimensions": {str: str}, "stat": str?}, ...]
}
JSON 외 prose / 코드 펜스 금지. 쿼리는 source 별 3-6 개.
"""

_SUMMARY_SYSTEM = """\
당신은 호스트/인프라 메트릭 이상치 결과를 사람에게 보고하는 분석가입니다.
입력은 anomaly summary (각 항목은 name, source ("prom"|"cw"), n_points, anomalies(ts/value/z/reason)).

먼저 reasoning 을 한국어 2~3 문장으로 작성:
"수치를 본 결과 X 시점에 Y 메트릭이 z=N.NN 으로 튀었고 ... 이는 ... 로 해석한다" 식으로
구체적 수치와 시점을 인용하면서 finding 으로 결론짓는 추론 흐름을 보여 주세요.

그 다음 finding 배열을 출력:
- title: 사람이 읽는 짧은 제목
- severity: "info" | "warn" | "error"
- evidence: 입력 anomalies 의 핵심 원소 일부 + 짧은 근거 텍스트

출력은 JSON 한 객체만:
{"reasoning": "...", "findings": [{"title":..., "severity":..., "evidence":[...]}, ...]}
JSON 외 금지.
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


def _plan(state: AnalysisState) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
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
    default = {
        "reasoning": "기본 쿼리 셋(CPU/Mem/Disk/Net + EC2/RDS CW)을 폴백으로 실행합니다.",
        "prom_queries": _DEFAULT_PROM_QUERIES,
        "cw_queries": _default_cw_queries(),
    }
    obj = llm_json(_PLAN_SYSTEM, user, default=default)
    if not isinstance(obj, dict):
        return default["prom_queries"], default["cw_queries"], default["reasoning"]
    prom_qs = obj.get("prom_queries") or default["prom_queries"]
    cw_qs = obj.get("cw_queries") or default["cw_queries"]
    reasoning = obj.get("reasoning") or default["reasoning"]
    return prom_qs, cw_qs, reasoning


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


def _summarize(summary: list[dict[str, Any]]) -> tuple[list[Finding], str]:
    if not any(s["anomalies"] for s in summary):
        return [], "이상 지점이 통계적으로 탐지되지 않아 finding 을 만들지 않았습니다."
    fallback = {
        "reasoning": "LLM 요약 실패로, 메트릭별 anomaly 개수만으로 fallback finding 을 생성했습니다.",
        "findings": [
            {
                "title": f"[{s['source']}] {s['name']} anomalies={len(s['anomalies'])}",
                "severity": "warn",
                "evidence": s["anomalies"],
            }
            for s in summary if s["anomalies"]
        ],
    }
    obj = llm_json(_SUMMARY_SYSTEM, str(summary), default=fallback)
    if not isinstance(obj, dict):
        obj = fallback
    items = obj.get("findings") or fallback["findings"]
    reasoning = obj.get("reasoning") or fallback["reasoning"]
    if not isinstance(items, list):
        items = fallback["findings"]

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
    return findings, reasoning


def run(state: AnalysisState) -> AnalysisState:
    events: list[dict] = [trace("os_subgraph", "enter", phase="enter")]

    t0 = time.time()
    prom_qs, cw_qs, plan_reasoning = _plan(state)
    events.append(trace(
        "os.plan",
        f"prom_queries={len(prom_qs)} cw_queries={len(cw_qs)}",
        phase="thought",
        reasoning=plan_reasoning,
        detail={
            "prom": [q.get("name") for q in prom_qs],
            "cw":   [q.get("name") for q in cw_qs],
        },
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    series_map = _fetch(state, prom_qs, cw_qs)
    nonempty = sum(1 for v in series_map.values() if v["series"])
    fetch_reasoning = (
        f"MCP `prometheus-query___prometheus_query`, `cloudwatch-metrics___cloudwatch_get_metric_data` "
        f"두 도구로 총 {len(series_map)}개 시계열을 요청했고 {nonempty}개에서 데이터가 들어왔습니다. "
        f"빈 시계열은 EC2/RDS 메트릭이 해당 윈도에 없거나 dimensions 매칭 실패 가능성이 있습니다."
    )
    events.append(trace(
        "os.fetch",
        f"series filled={nonempty}/{len(series_map)}",
        phase="thought",
        reasoning=fetch_reasoning,
        detail={"by_source": {n: len(v["series"]) for n, v in series_map.items()}},
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    summary = _anomaly_summary(series_map)
    n_anom = sum(len(s["anomalies"]) for s in summary)
    events.append(trace(
        "os.anomaly",
        f"anomalies={n_anom} across {len(summary)} series",
        reasoning=(
            f"z-score + EWMA 잔차 + change-point 결합으로 총 {n_anom}개의 이상 지점을 추출했습니다. "
            f"이 단계는 결정론적 코드라 LLM 호출 없이 빠르게 끝납니다."
        ),
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    findings, summarize_reasoning = _summarize(summary)
    events.append(trace(
        "os.summarize",
        f"findings={len(findings)}",
        phase="thought",
        reasoning=summarize_reasoning,
        duration_ms=int((time.time() - t0) * 1000),
    ))

    return {"os_findings": findings, "trace": events}
