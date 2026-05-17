"""Swarm 모드 뷰 — 카드형 메시지 + tool_call/tool_result 매칭 + streaming 실시간 갱신."""

from __future__ import annotations

import json
from typing import Any, Iterator

import streamlit as st


_AGENT_AVATAR = {
    "os_specialist":  "🖥️",
    "db_specialist":  "🗄️",
    "log_specialist": "📜",
}

_ROLE_AVATAR = {
    "human":  "🙋",
    "user":   "🙋",
    "ai":     "🤖",
    "tool":   "🛠️",
    "system": "ℹ️",
}


def _agent_chip(name: str | None) -> str:
    if not name:
        return "🤖 _(unnamed)_"
    icon = _AGENT_AVATAR.get(name, "🤖")
    return f"{icon} `{name}`"


def _is_handoff_tool(name: str | None) -> bool:
    return bool(name) and (name.startswith("transfer_to_") or name.startswith("handoff_to_"))


def _short_args(args: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "…"


def _render_message(m: dict, *, container=None) -> None:
    """한 메시지 카드 렌더. container 가 주어지면 그 안에 (placeholder.container() 등)."""
    target = container if container is not None else st

    role = m.get("role") or "ai"
    name = m.get("name")
    text = m.get("text") or ""
    tool_calls = m.get("tool_calls") or []
    tool_call_id = m.get("tool_call_id")

    # human
    if role in ("human", "user"):
        with target.chat_message("user", avatar="🙋"):
            target.markdown(text or "_(empty)_")
        return

    # tool result
    if role == "tool":
        with target.chat_message("assistant", avatar="🛠️"):
            target.caption(f"🛠️ tool result · `{name or '?'}`" + (f" · id=`{tool_call_id}`" if tool_call_id else ""))
            try:
                obj = json.loads(text) if text else {}
                target.json(obj, expanded=False)
            except Exception:
                target.code(text or "(empty)", language="json")
        return

    # ai (specialist)
    avatar = _AGENT_AVATAR.get(name, "🤖")
    with target.chat_message("assistant", avatar=avatar):
        if name:
            target.markdown(f"**{_agent_chip(name)}**")
        if text:
            target.markdown(text)

        for tc in tool_calls:
            tname = tc.get("name") or "?"
            args = tc.get("args")
            if _is_handoff_tool(tname):
                target.markdown(f"➡️ **handoff** · `{tname}`  ·  {_short_args(args)}")
            else:
                target.markdown(f"🛠️ **tool_call** · `{tname}`")
                with target.expander("arguments", expanded=False):
                    target.code(_short_args(args, limit=2000), language="json")


# ───────────────────────── 비스트리밍 (기존 호환) ─────────────────────────


def render(result: dict, request: dict | None = None) -> None:
    """이미 받아둔 swarm 결과(dict) 를 한꺼번에 렌더."""
    if "error" in result:
        st.error(result["error"])
        return

    handoffs = result.get("handoffs") or []
    final = result.get("final_active_agent") or "(unknown)"
    aborted = result.get("aborted")

    cols = st.columns([3, 1, 1, 1])
    if request:
        tr = request.get("time_range") or {}
        cols[0].markdown(
            f"**lens=`{request.get('lens','?')}`** · `{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`  \n"
            f"target: {', '.join(request.get('targets') or []) or '—'}"
        )
    cols[1].metric("핸드오프", max(0, len(handoffs) - 1))
    cols[2].metric("최종 specialist", final.split("_")[0] if "_" in final else final)
    if aborted:
        cols[3].metric("⚠️ 중단", aborted)

    if handoffs:
        st.divider()
        st.markdown("### 🔁 핸드오프 시퀀스")
        st.markdown(" → ".join(_agent_chip(a) for a in handoffs))

    st.divider()
    st.markdown("### 💬 Specialist 대화")
    msgs = result.get("messages") or []
    if not msgs:
        st.info("메시지 없음.")
        return
    for m in msgs:
        _render_message(m)


# ───────────────────────── Streaming ─────────────────────────


def render_stream(events: Iterator[dict], request: dict | None = None) -> dict:
    """invoke_stream() 의 NDJSON 이벤트를 받아 실시간 렌더하고, 누적 결과를 반환.

    반환 dict 는 비스트리밍 render() 의 입력과 동일한 구조 (messages/handoffs/final/aborted).
    """
    # ── 헤더 placeholder ──
    header_box = st.container()
    cols = header_box.columns([3, 1, 1, 1])
    if request:
        tr = request.get("time_range") or {}
        cols[0].markdown(
            f"**lens=`{request.get('lens','?')}`** · `{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`  \n"
            f"target: {', '.join(request.get('targets') or []) or '—'}"
        )
    handoff_metric = cols[1].empty()
    active_metric = cols[2].empty()
    abort_metric = cols[3].empty()

    handoffs: list[str] = []
    handoff_metric.metric("핸드오프", 0)

    # ── 핸드오프 chip 영역 ──
    st.divider()
    st.markdown("### 🔁 핸드오프 시퀀스")
    handoff_chip_box = st.empty()
    handoff_chip_box.caption("(시작 전)")

    st.divider()
    st.markdown("### 💬 Specialist 대화 (실시간)")
    log_box = st.container()  # 메시지가 누적될 컨테이너

    messages: list[dict] = []
    aborted: str | None = None
    final_active: str | None = None
    err: str | None = None

    status_box = st.empty()
    status_box.caption("⏳ 대기 중...")

    n_messages = 0
    for ev in events:
        t = ev.get("type")

        if t == "start":
            status_box.caption("▶ 분석 시작")
        elif t == "handoff":
            agent = ev.get("agent") or "?"
            handoffs.append(agent)
            handoff_metric.metric("핸드오프", max(0, len(handoffs) - 1))
            active_metric.metric(
                "현재 specialist",
                agent.split("_")[0] if "_" in agent else agent,
            )
            handoff_chip_box.markdown(" → ".join(_agent_chip(a) for a in handoffs))
            status_box.caption(f"➡️ 핸드오프 → {_agent_chip(agent)}")
        elif t == "message":
            msg = ev.get("message") or {}
            messages.append(msg)
            n_messages += 1
            with log_box:
                _render_message(msg)
            status_box.caption(f"💬 메시지 {n_messages}건 누적")
        elif t == "abort":
            aborted = ev.get("reason")
            abort_metric.metric("⚠️ 중단", aborted or "abort")
            status_box.warning(f"⚠️ 중단: {aborted}")
        elif t == "error":
            err = ev.get("error")
            status_box.error(f"❌ {err}")
            break
        elif t == "done":
            final_active = ev.get("final_active_agent")
            if final_active:
                active_metric.metric(
                    "최종 specialist",
                    final_active.split("_")[0] if "_" in final_active else final_active,
                )
            status_box.success(f"✅ 완료 · 메시지 {n_messages}건 · 핸드오프 {max(0, len(handoffs) - 1)}회")

    return {
        "messages": messages,
        "handoffs": handoffs,
        "final_active_agent": final_active,
        "aborted": aborted,
        **({"error": err} if err else {}),
    }
