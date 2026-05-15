"""AnalysisState — 모든 노드가 공유하는 LangGraph state schema."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage

Route = Literal["os", "db", "log", "multi"]


class AnalysisRequest(TypedDict, total=False):
    time_range: dict[str, str]
    targets: list[str]
    lens: str
    free_text: str


class Finding(TypedDict, total=False):
    id: str
    domain: Literal["os", "db", "log"]
    title: str
    severity: Literal["info", "warn", "error"]
    evidence: list[dict[str, Any]]
    timestamp: str


class Hypothesis(TypedDict, total=False):
    id: str
    statement: str
    supporting_finding_ids: list[str]
    confidence: float


class AnalysisReport(TypedDict, total=False):
    request: AnalysisRequest
    findings: list[Finding]
    hypotheses: list[Hypothesis]
    next_actions: list[str]
    markdown: str


class AnalysisState(TypedDict, total=False):
    request: AnalysisRequest
    route: Route
    os_findings: list[Finding] | None
    db_findings: list[Finding] | None
    log_findings: list[Finding] | None
    raw_signals: dict[str, Any]
    hypotheses: list[Hypothesis] | None
    report: AnalysisReport | None
    messages: list[BaseMessage]
    tool_budget: int
