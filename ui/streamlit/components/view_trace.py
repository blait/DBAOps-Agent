"""Thought Process 뷰 — Agent 의 실제 추론 흐름 (chat-like)."""

from __future__ import annotations

import streamlit as st


_PHASE_ICON = {
    "enter":   "▶",
    "exit":    "■",
    "thought": "💭",
    "warn":    "⚠️",
    "error":   "❌",
    "info":    "•",
}


_NODE_LABEL = {
    "router":         "🧭 Router",
    "os_subgraph":    "🖥️ OS subgraph",
    "db_subgraph":    "🗄️ DB subgraph",
    "log_subgraph":   "📜 Log subgraph",
    "os.plan":        "🖥️ OS · 계획",
    "os.fetch":       "🖥️ OS · MCP 호출",
    "os.anomaly":     "🖥️ OS · 이상 탐지",
    "os.summarize":   "🖥️ OS · 요약",
    "db.plan":        "🗄️ DB · 계획",
    "db.fetch":       "🗄️ DB · MCP 호출",
    "db.correlate":   "🗄️ DB · 상관 분석",
    "db.summarize":   "🗄️ DB · 요약",
    "log.plan":       "📜 Log · 계획",
    "log.fetch":      "📜 Log · MCP 호출",
    "log.classify":   "📜 Log · 템플릿 추출",
    "log.rca":        "📜 Log · RCA",
    "hypothesis":     "💡 Hypothesis",
    "reporter":       "📤 Reporter",
}


def _label(node: str) -> str:
    return _NODE_LABEL.get(node, node)


def render(report: dict) -> None:
    trace = report.get("trace") or []
    if not trace:
        st.info("trace 정보 없음.")
        return

    request = report.get("request") or {}
    free_text = (request.get("free_text") or "").strip()
    lens = request.get("lens", "?")
    targets = request.get("targets") or []

    # ── 사용자 발화 ──
    with st.chat_message("user", avatar="🙋"):
        if free_text:
            st.markdown(free_text)
        st.caption(
            f"lens=`{lens}` · targets={', '.join(targets) or '—'} · "
            f"window={(request.get('time_range') or {}).get('start','?')[:19]} → "
            f"{(request.get('time_range') or {}).get('end','?')[:19]}"
        )

    # ── 노드별 추론 흐름 ──
    total_ms = sum(ev.get("duration_ms", 0) or 0 for ev in trace) or 1
    max_ms = max((ev.get("duration_ms", 0) or 0) for ev in trace) or 1

    for ev in trace:
        node = ev.get("node", "?")
        phase = ev.get("phase", "info")
        icon = _PHASE_ICON.get(phase, "•")
        summary = ev.get("summary", "")
        reasoning = ev.get("reasoning", "")
        ms = ev.get("duration_ms")

        # subgraph enter 는 노이즈 — 작은 separator 만
        if phase == "enter":
            st.markdown(f"---\n##### {_label(node)}")
            continue

        avatar = "🤖" if phase == "thought" else icon
        with st.chat_message("assistant", avatar=avatar):
            header = f"**{_label(node)}** · `{summary}`"
            if ms is not None:
                bar_len = int(20 * ((ms or 0) / max_ms)) if ms else 0
                bar = "▰" * bar_len + "▱" * (20 - bar_len)
                header += f"  \n<span style='color:#888;font-size:90%'>{bar} {ms}ms</span>"
            st.markdown(header, unsafe_allow_html=True)
            if reasoning:
                st.markdown(reasoning)
            detail = ev.get("detail")
            if detail:
                with st.expander("세부 데이터"):
                    st.json(detail, expanded=False)

    # ── 마지막 정리 ──
    findings = report.get("findings") or []
    hypotheses = report.get("hypotheses") or []
    with st.chat_message("assistant", avatar="✅"):
        st.markdown(
            f"**최종 정리**  \n"
            f"- finding **{len(findings)}건** "
            f"(error {sum(1 for f in findings if f.get('severity')=='error')} · "
            f"warn {sum(1 for f in findings if f.get('severity')=='warn')} · "
            f"info {sum(1 for f in findings if f.get('severity')=='info')})  \n"
            f"- hypothesis **{len(hypotheses)}건**  \n"
            f"- 총 분석 시간 **{total_ms/1000:.1f}s**"
        )
