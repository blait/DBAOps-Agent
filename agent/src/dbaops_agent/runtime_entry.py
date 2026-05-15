"""AgentCore Runtime invocation entrypoint.

AgentCore SDK harness 가 이 모듈을 import 하여 invoke 한다.
실제 invocation contract 는 SDK 버전에 맞춰 채운다.
"""

from __future__ import annotations

import json
import logging
import os

from .graph import compile_graph
from .state import AnalysisState

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = compile_graph()
    return _GRAPH


def handler(event: dict, context: dict | None = None) -> dict:
    """AgentCore Runtime invoke handler.

    event 형태 (예시):
      {"request": {"time_range": {...}, "targets": [...], "lens": "os", "free_text": "..."}}
    """
    logger.info("invoke: %s", json.dumps(event)[:500])

    initial: AnalysisState = {
        "request": event.get("request", {}),
        "raw_signals": {},
        "messages": [],
        "tool_budget": int(os.environ.get("TOOL_BUDGET", "32")),
    }
    final = _get_graph().invoke(initial)
    return {"report": final.get("report")}


if __name__ == "__main__":
    # 로컬 smoke test
    out = handler({"request": {"free_text": "smoke test"}})
    print(json.dumps(out, ensure_ascii=False, indent=2))
