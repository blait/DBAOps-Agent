"""LangGraph StateGraph 정의 — router → 3 subgraphs → hypothesis → reporter."""

from __future__ import annotations

import logging
import os
from typing import Any, Iterator

from langgraph.graph import END, START, StateGraph

from .nodes import db_subgraph, hypothesis, log_subgraph, os_subgraph, reporter, router
from .state import AnalysisState

logger = logging.getLogger(__name__)


def build_graph() -> StateGraph:
    g = StateGraph(AnalysisState)

    g.add_node("router", router.run)
    g.add_node("os_subgraph", os_subgraph.run)
    g.add_node("db_subgraph", db_subgraph.run)
    g.add_node("log_subgraph", log_subgraph.run)
    g.add_node("hypothesis", hypothesis.run)
    g.add_node("reporter", reporter.run)

    g.add_edge(START, "router")

    def _route(state: AnalysisState) -> list[str]:
        route = state.get("route", "multi")
        if route == "os":
            return ["os_subgraph"]
        if route == "db":
            return ["db_subgraph"]
        if route == "log":
            return ["log_subgraph"]
        return ["os_subgraph", "db_subgraph", "log_subgraph"]

    g.add_conditional_edges("router", _route)

    # 모든 서브그래프 → hypothesis 로 fan-in
    g.add_edge("os_subgraph", "hypothesis")
    g.add_edge("db_subgraph", "hypothesis")
    g.add_edge("log_subgraph", "hypothesis")
    g.add_edge("hypothesis", "reporter")
    g.add_edge("reporter", END)

    return g


_COMPILED = None


def compile_graph():
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph().compile()
    return _COMPILED


def _initial_state(request: dict) -> AnalysisState:
    return {
        "request": request,
        "raw_signals": {},
        "messages": [],
        "tool_budget": int(os.environ.get("TOOL_BUDGET", "32")),
        "trace": [],
    }


def iter_fast(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Fast 그래프를 stream 하며 의미 있는 이벤트를 yield.

    이벤트 타입:
      - {"type": "start"}
      - {"type": "node", "node": str, "kind": "enter"|"update", "summary": str, "trace": [...], "findings": [...]?}
      - {"type": "report", "report": <full report>}
      - {"type": "done"}
      - {"type": "error", "error": str}
    """
    yield {"type": "start"}
    initial = _initial_state(request)
    final_state: dict[str, Any] = {}
    try:
        # stream_mode="updates" — 각 노드가 반환한 dict 가 그대로 들어온다
        for upd in compile_graph().stream(initial, stream_mode="updates"):
            # upd 형태: {node_name: {key: value, ...}}
            for node, payload in upd.items():
                if not isinstance(payload, dict):
                    continue
                # 누적 final_state 갱신 (마지막 dict 가 reporter 결과)
                final_state.update(payload)
                ev: dict[str, Any] = {"type": "node", "node": node, "kind": "update"}
                # trace 가 함께 들어왔으면 그것만 추출 (UI 가 이걸 사용)
                if "trace" in payload:
                    ev["trace"] = payload["trace"]
                # 짧은 summary 만들기
                if "route" in payload:
                    ev["summary"] = f"route={payload['route']}"
                elif node.endswith("_subgraph") or node in {"os_subgraph", "db_subgraph", "log_subgraph"}:
                    finds_key = node.replace("_subgraph", "_findings")
                    n = len((payload.get(finds_key) or []))
                    ev["summary"] = f"{node}: findings={n}"
                    ev["findings_count"] = n
                elif node == "hypothesis":
                    ev["summary"] = f"hypotheses={len(payload.get('hypotheses') or [])}"
                elif node == "reporter":
                    rep = payload.get("report") or {}
                    ev["summary"] = (
                        f"findings={len(rep.get('findings') or [])} "
                        f"hypotheses={len(rep.get('hypotheses') or [])}"
                    )
                    yield ev
                    yield {"type": "report", "report": rep}
                    continue
                else:
                    ev["summary"] = node
                yield ev
    except Exception as e:  # noqa: BLE001
        logger.exception("fast stream failed")
        yield {"type": "error", "error": str(e)}
        return

    yield {"type": "done"}
