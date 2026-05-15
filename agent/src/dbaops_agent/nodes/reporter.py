"""reporter_node — JSON + Markdown 리포트 조립 (deterministic, LLM 미사용)."""

from __future__ import annotations

from typing import Iterable

from ..state import AnalysisReport, AnalysisState, Finding, Hypothesis

_SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def _severity_rank(f: Finding) -> int:
    return _SEVERITY_ORDER.get(f.get("severity", "info"), 9)


def _gather_findings(state: AnalysisState) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(state.get("os_findings") or [])
    findings.extend(state.get("db_findings") or [])
    findings.extend(state.get("log_findings") or [])
    findings.sort(key=lambda f: (_severity_rank(f), f.get("domain", "z")))
    return findings


def _next_actions(findings: Iterable[Finding], hypotheses: Iterable[Hypothesis]) -> list[str]:
    actions: list[str] = []
    for f in findings:
        for ev in f.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("next_actions"):
                for a in ev["next_actions"]:
                    if isinstance(a, str) and a not in actions:
                        actions.append(a)
    if any(h.get("statement") for h in hypotheses):
        if "관련 로그/지표 시점 정렬 후 RCA 후보 검증" not in actions:
            actions.append("관련 로그/지표 시점 정렬 후 RCA 후보 검증")
    return actions[:10]


def _render_markdown(report: AnalysisReport) -> str:
    findings = report.get("findings") or []
    hypotheses = report.get("hypotheses") or []
    next_actions = report.get("next_actions") or []
    request = report.get("request") or {}

    lines: list[str] = ["# DBAOps Analysis Report", ""]
    tr = request.get("time_range") or {}
    lines.append(f"- 시간 범위: `{tr.get('start', '?')}` ~ `{tr.get('end', '?')}`")
    if request.get("targets"):
        lines.append(f"- 대상: {', '.join(request['targets'])}")
    if request.get("lens"):
        lines.append(f"- lens: `{request['lens']}`")
    lines.append("")

    if findings:
        by_domain: dict[str, list[Finding]] = {}
        for f in findings:
            by_domain.setdefault(f.get("domain", "?"), []).append(f)
        lines.append("## Findings")
        for domain, items in by_domain.items():
            lines.append(f"### {domain}")
            for f in items:
                sev = f.get("severity", "info").upper()
                lines.append(f"- **[{sev}]** {f.get('title', '')} _(id: {f.get('id')})_")
        lines.append("")
    else:
        lines.append("_탐지된 finding 없음._")
        lines.append("")

    if hypotheses:
        lines.append("## Hypotheses")
        for h in hypotheses:
            conf = h.get("confidence", 0.0)
            ids = ", ".join(h.get("supporting_finding_ids") or [])
            lines.append(f"- {h.get('statement', '')} _(conf {conf:.2f}; refs: {ids or '—'})_")
        lines.append("")

    if next_actions:
        lines.append("## Next Actions")
        for a in next_actions:
            lines.append(f"- {a}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def run(state: AnalysisState) -> AnalysisState:
    findings = _gather_findings(state)
    hypotheses = state.get("hypotheses") or []
    report: AnalysisReport = {
        "request": state.get("request") or {},
        "findings": findings,
        "hypotheses": hypotheses,
        "next_actions": _next_actions(findings, hypotheses),
    }
    report["markdown"] = _render_markdown(report)
    return {"report": report}
