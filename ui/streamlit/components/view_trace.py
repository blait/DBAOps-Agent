"""Thought Process 뷰 — 노드별 timeline + duration bar."""

from __future__ import annotations

import streamlit as st


_PHASE_ICON = {"enter": "▶", "exit": "■", "warn": "⚠", "error": "✗", "info": "•"}


def render(report: dict) -> None:
    trace = report.get("trace") or []
    if not trace:
        st.info("trace 정보 없음.")
        return

    total_ms = sum(ev.get("duration_ms", 0) or 0 for ev in trace) or 1
    st.caption(f"총 {len(trace)} events · {total_ms/1000:.1f}s")

    # 막대 길이 정규화
    max_ms = max((ev.get("duration_ms", 0) or 0) for ev in trace) or 1

    for ev in trace:
        ms = ev.get("duration_ms")
        phase = ev.get("phase", "info")
        icon = _PHASE_ICON.get(phase, "•")
        node = ev.get("node", "?")
        summary = ev.get("summary", "")
        bar_len = int(20 * ((ms or 0) / max_ms)) if ms else 0
        bar = "▰" * bar_len + "▱" * (20 - bar_len)

        cols = st.columns([1, 4, 1])
        cols[0].markdown(f"{icon} **{node}**")
        cols[1].markdown(f"{summary}")
        cols[2].markdown(f"`{bar}` `{ms or 0:>6}ms`")

        detail = ev.get("detail")
        if detail:
            with st.expander("detail"):
                st.json(detail, expanded=False)
