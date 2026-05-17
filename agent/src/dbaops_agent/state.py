"""AnalysisState — 모든 노드가 공유하는 LangGraph state schema.

LangGraph 의 병렬 실행 (multi 라우트) 에서는 같은 키를 여러 노드가 동시에 반환할 수 있다.
- raw_signals (dict) / tool_budget (int) 는 reducer 로 머지/감산하도록 Annotated 처리.
- trace (list) 는 노드별 입출력 요약을 누적 (extend reducer).
- domain 별 findings 는 서로 다른 키라 reducer 없이도 안전.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage

Route = Literal["os", "db", "log", "multi"]


def _merge_dict(left: dict | None, right: dict | None) -> dict:
    out: dict = {}
    if left:
        out.update(left)
    if right:
        out.update(right)
    return out


def _min_int(left: int | None, right: int | None) -> int:
    if left is None:
        return right or 0
    if right is None:
        return left
    return min(left, right)


def _extend_list(left: list | None, right: list | None) -> list:
    out: list = []
    if left:
        out.extend(left)
    if right:
        out.extend(right)
    return out


class TraceEvent(TypedDict, total=False):
    ts: str
    node: str
    phase: Literal["enter", "exit", "info", "warn", "error", "thought"]
    summary: str
    reasoning: str  # LLM 내러티브 (사용자에게 한국어로 보여주는 추론 흐름)
    detail: dict[str, Any]
    duration_ms: int


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
    trace: list[TraceEvent]


class AnalysisState(TypedDict, total=False):
    request: AnalysisRequest
    route: Route
    os_findings: list[Finding] | None
    db_findings: list[Finding] | None
    log_findings: list[Finding] | None
    raw_signals: Annotated[dict[str, Any], _merge_dict]
    hypotheses: list[Hypothesis] | None
    report: AnalysisReport | None
    messages: list[BaseMessage]
    tool_budget: Annotated[int, _min_int]
    trace: Annotated[list[TraceEvent], _extend_list]
