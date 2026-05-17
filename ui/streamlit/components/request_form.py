from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st


def build_request() -> dict:
    now = datetime.now(timezone.utc)
    default_start = now - timedelta(hours=1)

    mode = st.radio(
        "분석 모드",
        options=["fast", "swarm"],
        format_func=lambda v: {
            "fast":  "⚡ Fast (정해진 그래프)",
            "swarm": "🐝 Swarm (specialist 자율 핸드오프, 느리지만 깊음)",
        }[v],
        index=0,
        horizontal=True,
        help="fast 는 router→OS/DB/Log subgraph→hypothesis→reporter 정해진 흐름. "
             "swarm 은 3 specialist 가 ReAct 루프로 자율 핸드오프하며 follow-up 도구 호출.",
    )
    lens = st.selectbox("분석 lens", ["multi", "os", "db", "log"], index=0)
    start = st.text_input("Start (UTC ISO)", default_start.isoformat(timespec="seconds"))
    end = st.text_input("End (UTC ISO)", now.isoformat(timespec="seconds"))
    targets = st.text_input("대상 (콤마 구분)", "ec2-prometheus")
    free_text = st.text_area(
        "자연어 요청",
        "최근 1시간 동안 인스턴스 응답이 느렸다. 원인 후보를 알려줘.",
    )
    return {
        "mode": mode,
        "lens": lens,
        "time_range": {"start": start, "end": end},
        "targets": [t.strip() for t in targets.split(",") if t.strip()],
        "free_text": free_text,
    }
