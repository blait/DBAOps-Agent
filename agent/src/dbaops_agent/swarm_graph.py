"""Swarm 그래프 — 도메인별 specialist 3 + 자율 핸드오프.

Strands Agents 의 swarm 패턴과 동일한 컨셉.
사용 패키지: langgraph-swarm (https://github.com/langchain-ai/langgraph-swarm-py)

invoke_swarm  — 동기 호출, 최종 상태만 반환
iter_swarm    — generator, event 단위로 stream (thinking / tool_call / tool_result / handoff)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterator

from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph_swarm import create_handoff_tool, create_swarm

from .llm import get_llm
from .tools.mcp_tools import (
    DB_TOOLS,
    LOG_TOOLS,
    OS_TOOLS,
    infra_context,
)

logger = logging.getLogger(__name__)


def _system_for(name: str, role: str) -> str:
    ctx = infra_context()
    return f"""\
당신은 DBAOps 분석 swarm 의 {name} 전문가입니다.

[역할]
{role}

[인프라 컨텍스트]
- prom_instance_id  = {ctx['prom_instance_id']}
- aurora_writer_id  = {ctx['aurora_writer_id']}
- aurora_reader_id  = {ctx['aurora_reader_id']}
- aurora_cluster_id = {ctx['aurora_cluster_id']}
- mysql_db_id       = {ctx['mysql_db_id']}
- msk_cluster_name  = {ctx['msk_cluster_name']}
- log_bucket        = {ctx['log_bucket']}

[작동 규칙]
1. 자기 도메인 내에서 가능한 한 깊게 분석. 도구를 직접 호출하여 실제 수치를 확인.
2. 자기 도메인을 벗어나는 가설(예: OS 메트릭에서 DB 락 의심)이 떠오르면, 그쪽 specialist 에게 handoff_to_* 툴로 넘기세요.
3. 충분한 finding 을 모으고 더 이상 follow-up 이 필요 없다고 판단되면 최종 정리만 출력하고 멈추세요 (handoff 없이).
4. 한국어로 추론을 명시적으로 드러내며 작업하세요. 예: "메모리가 81%로 살짝 낮으니 DB 측 connection 폭증을 db_specialist 에게 확인 요청".
5. 이미 다른 specialist 가 확인한 데이터는 메시지 히스토리에서 참고하고 중복 호출하지 마세요.
6. 한 턴에 도구 호출은 1~2 개로 제한 — 같은 결과를 얻기 위해 여러 키 패턴을 동시에 시도하지 마세요. 하나 결과 보고 다음 결정.
"""


def _build_agent(name: str, role: str, tools: list, peers: list[tuple[str, str]]):
    """peers = [(agent_name, why_handoff), ...] — 다른 specialist 로 넘기는 도구를 자동 생성."""
    handoff_tools = [
        create_handoff_tool(agent_name=peer_name, description=why)
        for peer_name, why in peers
    ]
    return create_react_agent(
        model=get_llm(),
        tools=list(tools) + handoff_tools,
        prompt=SystemMessage(content=_system_for(name, role)),
        name=name,
    )


def build_swarm():
    """3 specialist swarm 을 컴파일해 반환."""

    os_specialist = _build_agent(
        name="os_specialist",
        role=(
            "호스트/인프라 메트릭 전문가. PromQL (node_exporter) 과 CloudWatch (AWS/EC2, AWS/RDS) "
            "를 사용해 CPU·메모리·디스크·네트워크 추세와 이상치를 분석합니다."
        ),
        tools=OS_TOOLS,
        peers=[
            ("db_specialist",
             "OS 메트릭에서 DB 영향(연결 수 · IOPS · TPS 등)이 의심되어 DB 내부 확인이 필요할 때 넘기세요."),
            ("log_specialist",
             "OS/HW 이상이 OS 또는 application 로그로 설명될 가능성이 있을 때 넘기세요."),
        ],
    )

    db_specialist = _build_agent(
        name="db_specialist",
        role=(
            "DBMS / Kafka 내부 성능 전문가. PG `pg_stat_*`, MySQL `performance_schema`, "
            "RDS Performance Insights, MSK CloudWatch 를 사용해 락·슬로우 쿼리·lag·ISR 등을 분석합니다."
        ),
        tools=DB_TOOLS,
        peers=[
            ("os_specialist",
             "DB 부하의 원인이 호스트/스토리지/네트워크 측에 있다고 의심되면 넘기세요."),
            ("log_specialist",
             "DB 에러 패턴 또는 application 로그 확인이 필요하면 넘기세요."),
        ],
    )

    log_specialist = _build_agent(
        name="log_specialist",
        role=(
            "로그 패턴/RCA 전문가. S3 의 PG/MySQL/Kafka 로그(.gz)를 정규식으로 검색하여 "
            "에러 패턴 빈도와 RCA 후보를 도출합니다."
        ),
        tools=LOG_TOOLS,
        peers=[
            ("os_specialist",
             "로그에서 호스트/네트워크 단서가 보이면 넘기세요."),
            ("db_specialist",
             "로그에서 DB 내부 검증 (락 / slow query / connection) 이 필요하면 넘기세요."),
        ],
    )

    swarm = create_swarm(
        agents=[os_specialist, db_specialist, log_specialist],
        default_active_agent=os.environ.get("DBAOPS_SWARM_ENTRY", "os_specialist"),
    ).compile(checkpointer=InMemorySaver())
    return swarm


# ───────────────────────── 메시지 정규화 ─────────────────────────


def _flatten_text(content: Any) -> str:
    """Anthropic content blocks 또는 str 을 평문 텍스트로."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    txt = c.get("text") or ""
                    if txt:
                        parts.append(txt)
                elif t == "tool_use":
                    # tool_use 블록은 message.tool_calls 로 별도 추출되므로 텍스트엔 포함 안 함
                    pass
                elif t == "tool_result":
                    # tool_result 블록 (역시 별도 ToolMessage 로 들어옴)
                    pass
                elif "text" in c:
                    parts.append(str(c.get("text")))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _normalize_tool_calls(m: Any) -> list[dict]:
    """LangChain Message.tool_calls + content 안의 tool_use 블록 둘 다 흡수."""
    calls: list[dict] = []
    for tc in (getattr(m, "tool_calls", None) or []):
        calls.append({
            "id":   tc.get("id"),
            "name": tc.get("name"),
            "args": tc.get("args"),
        })
    content = getattr(m, "content", None)
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                calls.append({
                    "id":   c.get("id"),
                    "name": c.get("name"),
                    "args": c.get("input"),
                })
    # dedupe by id
    seen: set[str] = set()
    out: list[dict] = []
    for tc in calls:
        key = tc.get("id") or json.dumps(tc, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(tc)
    return out


def normalize_message(m: Any) -> dict:
    """LangChain Message → UI/JSON 친화 dict."""
    role = getattr(m, "type", None) or "ai"
    name = getattr(m, "name", None)
    content = getattr(m, "content", None)
    text = _flatten_text(content)
    tool_calls = _normalize_tool_calls(m)
    out: dict = {
        "role": role,
        "name": name,
        "text": text[:8000] if text else "",
        "tool_calls": tool_calls,
    }
    # ToolMessage 는 tool_call_id 가 있어 어떤 호출의 결과인지 매칭 가능
    tcid = getattr(m, "tool_call_id", None)
    if tcid:
        out["tool_call_id"] = tcid
    return out


# ───────────────────────── 호출 헬퍼 ─────────────────────────


_SWARM = None


def _get_swarm():
    global _SWARM
    if _SWARM is None:
        _SWARM = build_swarm()
    return _SWARM


def _user_text(request: dict[str, Any]) -> str:
    tr = (request.get("time_range") or {})
    return (
        f"분석 요청: {request.get('free_text','(없음)')}\n"
        f"lens: {request.get('lens','?')}\n"
        f"time_range: {tr.get('start','?')} → {tr.get('end','?')}\n"
        f"targets: {request.get('targets') or '—'}\n"
        f"\n위 요청에 대해 자기 도메인부터 분석을 시작하고, 필요하면 다른 specialist 에게 핸드오프 하세요. "
        f"최종적으로 모든 specialist 가 충분히 분석했다고 판단되면, 발견사항 / 가설 / 다음 확인 항목을 한국어로 정리해 마무리하세요."
    )


def iter_swarm(request: dict[str, Any], *,
               recursion_limit: int = 30,
               ping_pong_window: int = 6,
               ping_pong_min_unique: int = 2) -> Iterator[dict]:
    """swarm 을 stream 모드로 돌리며 의미 있는 이벤트를 yield 한다.

    이벤트 타입:
      - {"type": "start"}
      - {"type": "handoff", "agent": str}
      - {"type": "message", "message": <normalized>}    # 새 메시지 한 건이 추가될 때
      - {"type": "abort", "reason": str}
      - {"type": "done", "final_active_agent": str, "handoffs": [...], "n_messages": int}
      - {"type": "error", "error": str}
    """
    from langchain_core.messages import HumanMessage

    yield {"type": "start"}

    config = {
        "configurable": {"thread_id": request.get("session_id") or "default"},
        "recursion_limit": recursion_limit,
    }
    handoffs: list[str] = []
    seen_ids: set[str] = set()
    last_active: list[str] = []
    final_state: dict[str, Any] = {}

    try:
        for chunk in _get_swarm().stream(
            {"messages": [HumanMessage(content=_user_text(request))]},
            config=config,
            stream_mode="values",
        ):
            final_state = chunk

            # active_agent 변경 = 핸드오프
            active = chunk.get("active_agent")
            if active and (not last_active or last_active[-1] != active):
                last_active.append(active)
                handoffs.append(active)
                yield {"type": "handoff", "agent": active}

            # 신규 메시지 단위로 emit
            for m in (chunk.get("messages") or []):
                mid = getattr(m, "id", None) or id(m)
                key = str(mid)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                yield {"type": "message", "message": normalize_message(m)}

            # ping-pong 감지
            window = last_active[-ping_pong_window:]
            if len(window) >= ping_pong_window and len(set(window)) <= ping_pong_min_unique:
                logger.warning("ping-pong detected — aborting (window=%s)", window)
                yield {"type": "abort", "reason": "ping_pong"}
                break
    except Exception as e:  # noqa: BLE001
        logger.exception("swarm stream failed")
        yield {"type": "error", "error": str(e)}
        return

    yield {
        "type": "done",
        "final_active_agent": final_state.get("active_agent"),
        "handoffs": handoffs,
        "n_messages": len(final_state.get("messages") or []),
    }


def invoke_swarm(request: dict[str, Any], *,
                 recursion_limit: int = 30,
                 ping_pong_window: int = 6,
                 ping_pong_min_unique: int = 2) -> dict[str, Any]:
    """동기 swarm 호출 — 모든 이벤트를 모아 최종 결과 dict 반환 (호환용)."""
    messages: list[dict] = []
    handoffs: list[str] = []
    aborted: str | None = None
    final_active: str | None = None
    err: str | None = None

    for ev in iter_swarm(request, recursion_limit=recursion_limit,
                         ping_pong_window=ping_pong_window,
                         ping_pong_min_unique=ping_pong_min_unique):
        t = ev.get("type")
        if t == "message":
            messages.append(ev["message"])
        elif t == "handoff":
            handoffs.append(ev["agent"])
        elif t == "abort":
            aborted = ev.get("reason")
        elif t == "done":
            final_active = ev.get("final_active_agent")
        elif t == "error":
            err = ev.get("error")

    if err:
        return {"error": err, "handoffs": handoffs, "messages": messages}
    return {
        "messages": messages,
        "handoffs": handoffs,
        "final_active_agent": final_active,
        "aborted": aborted,
    }
