"""기본 smoke test — graph 가 컴파일되고 invoke 가 빈 리포트를 반환."""

from __future__ import annotations

from dbaops_agent.graph import compile_graph


def test_graph_compiles_and_runs():
    graph = compile_graph()
    out = graph.invoke(
        {
            "request": {"free_text": "smoke", "lens": "os"},
            "raw_signals": {},
            "messages": [],
            "tool_budget": 8,
        }
    )
    assert "report" in out
    assert out["report"]["markdown"].startswith("# DBAOps Analysis Report")


def test_router_keyword():
    from dbaops_agent.nodes.router import run as router_run

    out = router_run({"request": {"free_text": "CPU spike"}})
    assert out["route"] == "os"
