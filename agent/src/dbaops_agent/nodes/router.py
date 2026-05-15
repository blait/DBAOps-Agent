"""router_node — 자연어 요청을 보고 어느 서브그래프로 갈지 결정.

1) request.lens 가 명시되면 그대로 사용
2) DBAOPS_OFFLINE=1 또는 LLM 실패 시 키워드 라우팅 fallback
3) 그 외에는 LLM(Opus 4.7) 라우팅
"""

from __future__ import annotations

from ..state import AnalysisState, Route
from ._common import llm_json

_KEYWORD_OS = ("cpu", "memory", "메모리", "disk", "iops", "host", "node", "디스크")
_KEYWORD_DB = ("query", "쿼리", "lock", "락", "tps", "qps", "kafka", "lag", "cache hit", "deadlock")
_KEYWORD_LOG = ("error", "에러", "log", "로그", "deadlock", "timeout", "panic")

_VALID: set[Route] = {"os", "db", "log", "multi"}

_ROUTER_SYSTEM = """\
You classify DBA/SRE analysis requests into ONE of: "os", "db", "log", "multi".
- os: host CPU/memory/disk/network metrics only.
- db: DBMS / Kafka internal metrics, locks, slow queries, lag.
- log: error/slow/audit logs only.
- multi: ambiguous OR needs cross-domain correlation.
Output ONLY a JSON object: {"route": "<one of the four>"}. No prose.
"""


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
    req = state.get("request") or {}
    explicit = req.get("lens")
    if explicit in _VALID:
        return {"route": explicit}  # type: ignore[return-value]

    free_text = req.get("free_text", "") or ""
    targets = req.get("targets") or []
    user_msg = f"free_text: {free_text}\ntargets: {targets}"

    obj = llm_json(_ROUTER_SYSTEM, user_msg, default=None)
    route = (obj or {}).get("route") if isinstance(obj, dict) else None
    if route in _VALID:
        return {"route": route}  # type: ignore[return-value]
    return {"route": _keyword_route(free_text)}  # type: ignore[return-value]
