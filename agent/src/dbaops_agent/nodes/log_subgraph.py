"""Log 서브그래프 — plan(LLM) → fetch(MCP) → classify(Drain3) → rca(LLM)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..analyzers.log_classify import classify, top_n
from ..state import AnalysisState, Finding
from ..tools.mcp_client import MCPClient
from ._common import llm_json, time_range, utc_iso

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = """\
You plan log fetches across PG / MySQL / Kafka log buckets.
Output ONLY a JSON object:
{"sources": [{"name": str, "bucket": str, "key": str, "regex": str|null}, ...]}
Use `<DEFAULT_BUCKET>` literal if you do not know the bucket name; the host will substitute.
3-5 sources max. No prose, no code fences.
"""

_RCA_SYSTEM = """\
You produce concise log RCA findings in Korean. Input is a JSON array
[{"source": str, "templates": [{"template": str, "count": int}, ...]}, ...].
Output ONLY a JSON array:
[{"title": str, "severity": "info"|"warn"|"error", "evidence": [...], "next_actions": [...]}, ...].
Highlight bursts and likely causal templates. No prose, no code fences.
"""

_DEFAULT_SOURCES = [
    {"name": "pg_error",    "bucket": "<DEFAULT_BUCKET>", "key": "postgres/error.log.gz",  "regex": "ERROR|FATAL|deadlock"},
    {"name": "mysql_error", "bucket": "<DEFAULT_BUCKET>", "key": "mysql/error.log.gz",     "regex": "\\[ERROR\\]|deadlock"},
    {"name": "kafka_server","bucket": "<DEFAULT_BUCKET>", "key": "kafka/server.log.gz",    "regex": "ERROR|ISR shrink|Under-Replicated"},
]


def _plan(state: AnalysisState) -> list[dict[str, str]]:
    req = state.get("request") or {}
    user = (
        f"time_range={req.get('time_range')}\n"
        f"targets={req.get('targets')}\n"
        f"free_text={req.get('free_text')}"
    )
    obj = llm_json(_PLAN_SYSTEM, user, default={"sources": _DEFAULT_SOURCES})
    sources = (obj or {}).get("sources") if isinstance(obj, dict) else None
    return sources or _DEFAULT_SOURCES


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


def _rca(classified: list[dict]) -> list[Finding]:
    if not any(c["templates"] for c in classified):
        return []
    fallback = [
        {
            "title": f"{c['source']} top template: {c['templates'][0]['template'][:80]}",
            "severity": "warn",
            "evidence": c["templates"][:5],
            "next_actions": [],
        }
        for c in classified
        if c["templates"]
    ]
    items = llm_json(_RCA_SYSTEM, str(classified), default=fallback) or fallback
    if not isinstance(items, list):
        items = fallback

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
    return findings


def run(state: AnalysisState) -> AnalysisState:
    sources = _plan(state)
    fetched = _fetch(state, sources)
    classified = _classify(fetched)
    findings = _rca(classified)
    return {"log_findings": findings}
