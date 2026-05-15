"""LangGraph StateGraph 정의 — router → 3 subgraphs → hypothesis → reporter."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import db_subgraph, hypothesis, log_subgraph, os_subgraph, reporter, router
from .state import AnalysisState


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


def compile_graph():
    return build_graph().compile()
