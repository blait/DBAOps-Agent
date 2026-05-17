"""router_node — 자연어 요청을 보고 어느 서브그래프로 갈지 결정.

1) request.lens 가 명시되면 그대로 사용
2) DBAOPS_OFFLINE=1 또는 LLM 실패 시 키워드 라우팅 fallback
3) 그 외에는 LLM(Opus 4.7) 라우팅
"""

from __future__ import annotations

import time

from ..state import AnalysisState, Route
from ._common import llm_json, trace

_KEYWORD_OS = ("cpu", "memory", "메모리", "disk", "iops", "host", "node", "디스크")
_KEYWORD_DB = ("query", "쿼리", "lock", "락", "tps", "qps", "kafka", "lag", "cache hit", "deadlock")
_KEYWORD_LOG = ("error", "에러", "log", "로그", "deadlock", "timeout", "panic")

_VALID: set[Route] = {"os", "db", "log", "multi"}

_ROUTER_SYSTEM = """\
당신은 DBA/SRE 분석 요청을 분류하는 라우터입니다.
사용자의 자연어 요청을 보고 다음 중 하나의 lens 로 분류하세요:
- "os":   호스트 CPU/메모리/디스크/네트워크 메트릭만 보면 충분한 경우
- "db":   DBMS / Kafka 내부 성능, 락, 슬로우 쿼리, lag 등 DB 면을 봐야 하는 경우
- "log":  Error / Slow / Audit 로그 패턴만 보면 충분한 경우
- "multi": 모호하거나, 도메인 간 교차 상관이 필요한 경우

출력은 반드시 JSON 객체 한 개:
{"route": "os|db|log|multi", "reasoning": "왜 이 lens 를 골랐는지 한국어 한두 문장. '사용자가 X 라고 했으므로 Y 가 의심되어 Z lens 가 적절하다' 식으로."}
JSON 외의 prose 나 코드 펜스 금지.
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
    t0 = time.time()
    req = state.get("request") or {}
    free_text = req.get("free_text", "") or ""
    targets = req.get("targets") or []

    explicit = req.get("lens")
    if explicit in _VALID:
        ms = int((time.time() - t0) * 1000)
        reasoning = (
            f"사용자가 lens 를 `{explicit}` 로 직접 지정했습니다. "
            f"요청 메시지({free_text[:80] or '—'})와 무관하게 그대로 따릅니다."
        )
        return {
            "route": explicit,  # type: ignore[typeddict-item]
            "trace": [trace("router", f"explicit lens={explicit}", phase="thought",
                            reasoning=reasoning,
                            detail={"source": "request.lens"}, duration_ms=ms)],
        }

    user_msg = f"free_text: {free_text}\ntargets: {targets}"
    obj = llm_json(_ROUTER_SYSTEM, user_msg, default=None)

    if isinstance(obj, dict) and obj.get("route") in _VALID:
        route = obj["route"]
        reasoning = obj.get("reasoning") or f"LLM 라우팅으로 `{route}` 선택."
        source = "llm"
    else:
        route = _keyword_route(free_text)
        reasoning = (
            f"LLM 라우팅이 실패하거나 거부되어 키워드 폴백을 사용했습니다. "
            f"`{free_text[:60]}` 안의 키워드 매칭 결과 `{route}` 로 분기합니다."
        )
        source = "keyword_fallback"

    ms = int((time.time() - t0) * 1000)
    return {
        "route": route,  # type: ignore[typeddict-item]
        "trace": [trace("router", f"route={route}", phase="thought",
                        reasoning=reasoning,
                        detail={"source": source, "free_text": free_text[:120]},
                        duration_ms=ms)],
    }
