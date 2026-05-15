"""DB 서브그래프 — plan → fetch_(pg|mysql|kafka) → correlate → summarize."""

from __future__ import annotations

from ..state import AnalysisState


def run(state: AnalysisState) -> AnalysisState:
    return {"db_findings": []}
