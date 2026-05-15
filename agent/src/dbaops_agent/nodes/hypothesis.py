"""hypothesis_node — 도메인 교차 상관 가설 생성 (조건부)."""

from __future__ import annotations

from ..state import AnalysisState


def run(state: AnalysisState) -> AnalysisState:
    findings = (
        (state.get("os_findings") or [])
        + (state.get("db_findings") or [])
        + (state.get("log_findings") or [])
    )
    if state.get("route") != "multi" and len(findings) < 2:
        return {"hypotheses": []}
    # Phase 4: LLM 호출로 시간축 정렬 + 인과 가설 생성
    return {"hypotheses": []}
