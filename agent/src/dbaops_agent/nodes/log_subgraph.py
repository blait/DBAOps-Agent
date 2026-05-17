"""Log 서브그래프 — plan(LLM) → fetch(MCP) → classify(Drain3) → rca(LLM)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import time

from ..analyzers.log_classify import classify, top_n
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, trace, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
당신은 PG / MySQL / Kafka 로그 분석가입니다. 사용자 요청에 맞춰
어떤 S3 버킷·키·정규식으로 로그를 가져올지 계획합니다.

출력은 JSON 한 객체:
{
  "reasoning": "사용자 요청에서 X 가 의심되어 Y 로그 소스에서 Z 패턴을 보겠다 — 한국어 1~2 문장",
  "sources": [{"name": str, "bucket": str, "key": str, "regex": str|null}, ...]
}
- bucket 이 모르면 `<DEFAULT_BUCKET>` 리터럴 사용 (host 가 치환).
- 최대 5 source. JSON 외 prose 금지.
"""

_RCA_SYSTEM = """\
당신은 로그 RCA 분석가입니다. 입력은 source 별 Drain3 템플릿 + 빈도 집계.

[reasoning 작성 규칙]
2~3 문장. **도구·source·템플릿 문자열·빈도**를 반드시 인용:
좋은 예) "s3_log_fetch(logs-burst/postgres/) 387건이 'ERROR: deadlock detected' 템플릿,
        388건이 'FATAL: too many connections' — deadlock 으로 인한 connection 누수가 의심된다."

[finding 작성 규칙]
- title 에 source + 빈도 + 템플릿 명시 — 예: "[pg_error] 'ERROR: deadlock detected' 387건 burst"
- evidence 첫 항목은 반드시 다음 dict:
    {"tool": "s3_log_fetch",
     "source": "<source name>",
     "template": "<Drain3 템플릿 문자열>",
     "count": int,
     "ratio_or_total": "<예: 387/1500>"}
- evidence 의 두 번째 이후엔 다른 빈발 템플릿이나 burst 시점 기록 첨부.

출력은 JSON 한 객체:
{
  "reasoning": "...",
  "findings": [
    {"title": str, "severity": "info"|"warn"|"error", "evidence": [...], "next_actions": [str, ...]}, ...
  ]
}
JSON 외 금지.
"""

_DEFAULT_SOURCES = [
    {"name": "pg_error",    "bucket": "<DEFAULT_BUCKET>", "key": "postgres/error.log.gz",  "regex": "ERROR|FATAL|deadlock"},
    {"name": "mysql_error", "bucket": "<DEFAULT_BUCKET>", "key": "mysql/error.log.gz",     "regex": "\\[ERROR\\]|deadlock"},
    {"name": "kafka_server","bucket": "<DEFAULT_BUCKET>", "key": "kafka/server.log.gz",    "regex": "ERROR|ISR shrink|Under-Replicated"},
]


def _plan(state: AnalysisState) -> tuple[list[dict[str, str]], str]:
    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}"
    )
    default = {
        "reasoning": "기본 PG/MySQL/Kafka 로그 소스 셋을 폴백으로 조회합니다.",
        "sources": _DEFAULT_SOURCES,
    }
    obj = llm_json(_PLAN_SYSTEM, user, default=default)
    if not isinstance(obj, dict):
        obj = default
    sources = obj.get("sources") or _DEFAULT_SOURCES
    reasoning = obj.get("reasoning") or default["reasoning"]
    return sources, reasoning


def _resolve_bucket(value: str, default_bucket: str) -> str:
    return default_bucket if value == "<DEFAULT_BUCKET>" else value


def _expand_keys(bucket: str, key_or_prefix: str, max_keys: int = 20) -> list[str]:
    """key 가 '/' 로 끝나거나 .gz/.log 가 아니면 prefix 로 보고 list_objects_v2 로 확장."""
    is_prefix = key_or_prefix.endswith("/") or not (
        key_or_prefix.endswith(".gz") or key_or_prefix.endswith(".log") or key_or_prefix.endswith(".txt")
    )
    if not is_prefix:
        return [key_or_prefix]
    try:
        import boto3

        s3 = boto3.client("s3")
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=key_or_prefix, MaxKeys=max_keys)
        keys = [obj["Key"] for obj in (resp.get("Contents") or []) if obj["Key"].endswith((".gz", ".log", ".txt"))]
        return keys
    except Exception as e:  # noqa: BLE001
        logger.warning("list_objects_v2 failed for s3://%s/%s: %s", bucket, key_or_prefix, e)
        return []


def _fetch(state: AnalysisState, sources: list[dict[str, str]]) -> list[dict]:
    import os

    cache = state.setdefault("raw_signals", {})
    budget = [state.get("tool_budget", 16)]
    client = MCPClient()
    default_bucket = os.environ.get("DEFAULT_LOG_BUCKET", "")
    out: list[dict] = []
    for s in sources:
        bucket = _resolve_bucket(s.get("bucket", ""), default_bucket)
        if not bucket:
            logger.warning("log source %s skipped — no bucket", s.get("name"))
            continue
        keys = _expand_keys(bucket, s.get("key", ""))
        if not keys:
            logger.warning("log source %s: no s3 keys under %s", s.get("name"), s.get("key"))
            continue
        merged_lines: list[str] = []
        for k in keys[:5]:  # 한 source 당 최대 5개 객체
            try:
                r = client.call(
                    "s3-log-fetch___s3_log_fetch",
                    {
                        "bucket": bucket,
                        "key": k,
                        "regex": s.get("regex"),
                        "max_lines": int(s.get("max_lines", 5000)),
                    },
                    cache=cache,
                    budget=budget,
                )
                merged_lines.extend((r or {}).get("lines") or [])
                if len(merged_lines) >= int(s.get("max_lines", 5000)):
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning("log_fetch %s/%s failed: %s", s.get("name"), k, e)
        out.append({"name": s["name"], "lines": merged_lines})
    state["tool_budget"] = budget[0]
    return out


def _classify(fetched: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src in fetched:
        templates = top_n(classify(src.get("lines") or []), n=15)
        out.append(
            {
                "source": src["name"],
                "templates": [{"template": t.template, "count": t.count} for t in templates],
            }
        )
    return out


def _rca(classified: list[dict]) -> tuple[list[Finding], str]:
    if not any(c["templates"] for c in classified):
        return [], "수집된 로그 라인이 없어 RCA 추론을 건너뛰었습니다."
    fb_findings = [
        {
            "title": f"{c['source']} top template: {c['templates'][0]['template'][:80]}",
            "severity": "warn",
            "evidence": c["templates"][:5],
            "next_actions": [],
        }
        for c in classified
        if c["templates"]
    ]
    fallback = {
        "reasoning": "LLM RCA 실패로, source 별 최상위 템플릿만 fallback finding 으로 발화했습니다.",
        "findings": fb_findings,
    }
    obj = llm_json(_RCA_SYSTEM, str(classified), default=fallback)
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
        ev = it.get("evidence") or []
        if it.get("next_actions"):
            ev = list(ev) + [{"next_actions": it["next_actions"]}]
        findings.append(
            {
                "id": str(uuid.uuid4())[:8],
                "domain": "log",
                "title": it.get("title", "(untitled)"),
                "severity": it.get("severity", "info"),
                "evidence": ev,
                "timestamp": utc_iso(0),
            }
        )
    return findings, reasoning


def run(state: AnalysisState) -> AnalysisState:
    events: list[dict] = [trace("log_subgraph", "enter", phase="enter")]

    t0 = time.time()
    sources, plan_reasoning = _plan(state)
    events.append(trace(
        "log.plan",
        f"sources={len(sources)}",
        phase="thought",
        reasoning=plan_reasoning,
        detail={"names": [s.get("name") for s in sources]},
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    fetched = _fetch(state, sources)
    total_lines = sum(len(s.get("lines") or []) for s in fetched)
    fetch_reasoning = (
        f"MCP `s3-log-fetch___s3_log_fetch` 로 {len(sources)} source 의 객체를 받아 "
        f"총 {total_lines} 줄을 수집했습니다 (regex 적용). 빈 source 는 prefix 매칭 실패 가능."
    )
    events.append(trace(
        "log.fetch",
        f"sources={len(fetched)} total_lines={total_lines}",
        phase="thought",
        reasoning=fetch_reasoning,
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    classified = _classify(fetched)
    n_templates = sum(len(c.get("templates") or []) for c in classified)
    events.append(trace(
        "log.classify",
        f"templates={n_templates}",
        reasoning=(
            f"Drain3 로 {total_lines} 줄을 {n_templates} 개 템플릿으로 묶었습니다. "
            "이 단계는 결정론적 코드라 LLM 호출 없이 처리됩니다."
        ),
        duration_ms=int((time.time() - t0) * 1000),
    ))

    t0 = time.time()
    findings, rca_reasoning = _rca(classified)
    events.append(trace(
        "log.rca",
        f"findings={len(findings)}",
        phase="thought",
        reasoning=rca_reasoning,
        duration_ms=int((time.time() - t0) * 1000),
    ))

    return {"log_findings": findings, "trace": events}
