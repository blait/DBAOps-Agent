"""router_node — 자연어 요청을 보고 어느 서브그래프로 갈지 결정."""

from __future__ import annotations

from ..state import AnalysisState, Route

_KEYWORD_OS = ("cpu", "memory", "메모리", "disk", "iops", "host", "node")
_KEYWORD_DB = ("query", "쿼리", "lock", "락", "tps", "qps", "kafka", "lag", "cache hit")
_KEYWORD_LOG = ("error", "에러", "log", "로그", "deadlock", "timeout")


def _keyword_route(text: str) -> Route:
    text = (text or "").lower()
    hits = {
        "os": any(k in text for k in _KEYWORD_OS),
        "db": any(k in text for k in _KEYWORD_DB),
        "log": any(k in text for k in _KEYWORD_LOG),
    }
    n = sum(hits.values())
    if n == 0 or n >= 2:
        return "multi"
    for k, v in hits.items():
        if v:
            return k  # type: ignore[return-value]
    return "multi"


def run(state: AnalysisState) -> AnalysisState:
    """Phase 1: 키워드 기반 라우팅. Phase 2에서 LLM 라우터로 교체."""
    req = state.get("request") or {}
    explicit = req.get("lens")
    if explicit in ("os", "db", "log", "multi"):
        return {"route": explicit}  # type: ignore[return-value]
    return {"route": _keyword_route(req.get("free_text", ""))}  # type: ignore[return-value]
