"""reporter_node — JSON + Markdown 리포트 조립 (deterministic)."""

from __future__ import annotations

from ..state import AnalysisReport, AnalysisState


def _render_markdown(report: AnalysisReport) -> str:
    findings = report.get("findings") or []
    hypotheses = report.get("hypotheses") or []
    parts = ["# DBAOps Analysis Report", ""]
    parts.append(f"- findings: {len(findings)}")
    parts.append(f"- hypotheses: {len(hypotheses)}")
    return "\n".join(parts)


def run(state: AnalysisState) -> AnalysisState:
    findings = (
        (state.get("os_findings") or [])
        + (state.get("db_findings") or [])
        + (state.get("log_findings") or [])
    )
    report: AnalysisReport = {
        "request": state.get("request") or {},
        "findings": findings,
        "hypotheses": state.get("hypotheses") or [],
        "next_actions": [],
    }
    report["markdown"] = _render_markdown(report)
    return {"report": report}
