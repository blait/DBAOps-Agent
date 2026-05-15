"""Log 서브그래프 — plan → fetch → classify → rca."""

from __future__ import annotations

from ..state import AnalysisState


def run(state: AnalysisState) -> AnalysisState:
    return {"log_findings": []}
