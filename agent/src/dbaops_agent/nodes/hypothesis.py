"""hypothesis_node — 도메인 교차 가설 생성 (조건부)."""

from __future__ import annotations

import logging
import uuid

import time

from ..analyzers.correlate import bucketize, cross_source
from ..state import AnalysisState, Finding, Hypothesis
from ._common import llm_json, trace, utc_iso

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a senior DBA building cross-domain hypotheses from OS/DB/log findings.
Input is a JSON object with {"findings": [...], "co_occurrences": [...]}.
co_occurrences entries are time buckets where multiple domains had findings.
Output ONLY a JSON array:
[{"statement": str, "supporting_finding_ids": [str, ...], "confidence": 0.0-1.0}, ...].
Prefer 1-3 hypotheses. Reference finding ids from input. No prose, no code fences.
"""


def _gather(state: AnalysisState) -> list[Finding]:
    return (
        (state.get("os_findings") or [])
        + (state.get("db_findings") or [])
        + (state.get("log_findings") or [])
    )


def _co_occurrence(findings: list[Finding]) -> list[dict]:
    """findings.timestamp 를 60초 윈도로 묶어 도메인 교차 발화를 찾는다."""
    by_source: dict[str, list[dict]] = {"os": [], "db": [], "log": []}
    for f in findings:
        ts = f.get("timestamp")
        if not ts:
            continue
        domain = f.get("domain")
        if domain in by_source:
            by_source[domain].append({"ts": ts, "id": f.get("id"), "title": f.get("title")})
    corr = bucketize(by_source, window_sec=60)
    cross = cross_source(corr, min_sources=2)
    return [{"bucket": c.bucket, "sources": {k: [e["id"] for e in v] for k, v in c.sources.items()}} for c in cross]


def run(state: AnalysisState) -> AnalysisState:
    t0 = time.time()
    findings = _gather(state)
    route = state.get("route")
    if route != "multi" and len(findings) < 2:
        return {
            "hypotheses": [],
            "trace": [trace("hypothesis",
                            f"skipped (route={route} findings={len(findings)})",
                            phase="exit",
                            duration_ms=int((time.time()-t0)*1000))],
        }

    co = _co_occurrence(findings)
    payload = {
        "findings": [
            {"id": f.get("id"), "domain": f.get("domain"), "title": f.get("title"), "severity": f.get("severity"), "ts": f.get("timestamp")}
            for f in findings
        ],
        "co_occurrences": co,
    }

    fallback: list[dict] = []
    if co:
        all_ids = [fid for c in co for ids in c["sources"].values() for fid in ids]
        fallback.append(
            {
                "statement": f"{len(co)} 시점에서 도메인 교차 이상이 동시 발화 — 동일 인과 의심.",
                "supporting_finding_ids": list(dict.fromkeys(all_ids))[:8],
                "confidence": 0.5,
            }
        )

    items = llm_json(_SYSTEM, str(payload), default=fallback) or fallback
    if not isinstance(items, list):
        items = fallback

    hypotheses: list[Hypothesis] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        hypotheses.append(
            {
                "id": str(uuid.uuid4())[:8],
                "statement": it.get("statement", "(empty)"),
                "supporting_finding_ids": it.get("supporting_finding_ids") or [],
                "confidence": float(it.get("confidence", 0.0)),
            }
        )
    # raw_signals 에 디버그용 보존 (옵션)
    state.setdefault("raw_signals", {})["hypothesis_co"] = co
    return {
        "hypotheses": hypotheses,
        "trace": [trace("hypothesis",
                        f"hypotheses={len(hypotheses)} co_buckets={len(co)} findings={len(findings)}",
                        phase="exit",
                        duration_ms=int((time.time()-t0)*1000))],
    }


# 사용되지 않는 import 정리용
_ = utc_iso
