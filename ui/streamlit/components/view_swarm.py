"""Swarm 모드 뷰 — handoff 시퀀스 + specialist 메시지 chat 흐름."""

from __future__ import annotations

import streamlit as st


_AGENT_AVATAR = {
    "os_specialist":  "🖥️",
    "db_specialist":  "🗄️",
    "log_specialist": "📜",
}

_ROLE_AVATAR = {
    "human": "🙋",
    "ai":    "🤖",
    "tool":  "🛠️",
    "system": "ℹ️",
}


def _agent_chip(name: str) -> str:
    icon = _AGENT_AVATAR.get(name, "🤖")
    return f"{icon} `{name}`"


def render(result: dict, request: dict | None = None) -> None:
    if "error" in result:
        st.error(result["error"])
        return

    handoffs = result.get("handoffs") or []
    final = result.get("final_active_agent") or "(unknown)"
    aborted = result.get("aborted")

    # ── 헤더 ──
    cols = st.columns([3, 1, 1, 1])
    if request:
        tr = request.get("time_range") or {}
        cols[0].markdown(
            f"**lens=`{request.get('lens','?')}`** · `{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`  \n"
            f"target: {', '.join(request.get('targets') or []) or '—'}"
        )
    cols[1].metric("핸드오프", len(handoffs) - 1 if len(handoffs) > 1 else 0)
    cols[2].metric("최종 specialist", final.split("_")[0] if "_" in final else final)
    if aborted:
        cols[3].metric("⚠️ 중단", aborted)

    # ── 핸드오프 시퀀스 ──
    if handoffs:
        st.divider()
        st.markdown("### 🔁 핸드오프 시퀀스")
        chain = " → ".join(_agent_chip(a) for a in handoffs)
        st.markdown(chain, unsafe_allow_html=True)

    st.divider()

    # ── 메시지 흐름 ──
    msgs = result.get("messages") or []
    if not msgs:
        st.info("메시지 없음.")
        return

    st.markdown("### 💬 Specialist 대화")
    for m in msgs:
        role = m.get("role") or "ai"
        name = m.get("name")
        content = m.get("content") or ""
        tool_calls = m.get("tool_calls") or []

        if role == "human":
            with st.chat_message("user", avatar=_ROLE_AVATAR["human"]):
                st.markdown(content)
            continue

        if role == "tool":
            with st.chat_message("assistant", avatar=_ROLE_AVATAR["tool"]):
                st.caption(f"🛠️ tool result · `{name or '?'}`")
                with st.expander("결과", expanded=False):
                    st.code(content, language="json")
            continue

        avatar = _AGENT_AVATAR.get(name, _ROLE_AVATAR.get(role, "🤖"))
        with st.chat_message("assistant", avatar=avatar):
            if name:
                st.caption(_agent_chip(name))
            if content:
                st.markdown(content)
            if tool_calls:
                with st.expander(f"🛠️ tool_calls ({len(tool_calls)})"):
                    for tc in tool_calls:
                        st.markdown(f"- **`{tc.get('name')}`** · args=`{tc.get('args')}`")
