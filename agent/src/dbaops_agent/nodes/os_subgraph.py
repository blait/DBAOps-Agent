"""OS 서브그래프 — plan → fetch → anomaly → summarize."""

from __future__ import annotations

from ..state import AnalysisState


def run(state: AnalysisState) -> AnalysisState:
    """Phase 1 placeholder — Phase 1 구현 시 plan/fetch/anomaly/summarize 단계 추가."""
    return {"os_findings": []}
