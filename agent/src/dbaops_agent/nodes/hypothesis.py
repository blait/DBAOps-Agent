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
당신은 시니어 DBA 입니다. OS / DB / Log 도메인의 finding 들을 보고
시간축 동시 발화(co_occurrences) 를 단서로 인과 가설을 만듭니다.

출력은 JSON 한 객체:
{
  "reasoning": "어떤 finding 들을 어떻게 묶었고, 왜 이 인과를 의심했는지를 한국어 2~4 문장으로 설명",
  "hypotheses": [
    {"statement": str, "supporting_finding_ids": [str, ...], "confidence": 0.0-1.0}, ...
  ]
}
- 1~3개 가설 권장.
- supporting_finding_ids 는 입력 finding 의 id 만 사용.
- JSON 외 prose / 코드 펜스 금지.
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
        skip_reason = (
            f"route={route} 단일 도메인이고 finding {len(findings)}건이라 "
            f"교차 가설 단계는 건너뜁니다."
        )
        return {
            "hypotheses": [],
            "trace": [trace("hypothesis",
                            f"skipped (route={route} findings={len(findings)})",
                            phase="thought",
                            reasoning=skip_reason,
                            duration_ms=int((time.time() - t0) * 1000))],
        }

    co = _co_occurrence(findings)
    payload = {
        "findings": [
            {"id": f.get("id"), "domain": f.get("domain"), "title": f.get("title"), "severity": f.get("severity"), "ts": f.get("timestamp")}
            for f in findings
        ],
        "co_occurrences": co,
    }

    fb_hyps: list[dict] = []
    if co:
        all_ids = [fid for c in co for ids in c["sources"].values() for fid in ids]
        fb_hyps.append(
            {
                "statement": f"{len(co)} 시점에서 도메인 교차 이상이 동시 발화 — 동일 인과 의심.",
                "supporting_finding_ids": list(dict.fromkeys(all_ids))[:8],
                "confidence": 0.5,
            }
        )
    fallback = {
        "reasoning": "LLM 가설 생성 실패로, 시간 동시 발화 버킷만으로 fallback 가설을 생성했습니다.",
        "hypotheses": fb_hyps,
    }

    obj = llm_json(_SYSTEM, str(payload), default=fallback)
    if not isinstance(obj, dict):
        obj = fallback
    items = obj.get("hypotheses") or fb_hyps
    reasoning = obj.get("reasoning") or fallback["reasoning"]
    if not isinstance(items, list):
        items = fb_hyps

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
                        phase="thought",
                        reasoning=reasoning,
                        duration_ms=int((time.time() - t0) * 1000))],
    }


# 사용되지 않는 import 정리용
_ = utc_iso
