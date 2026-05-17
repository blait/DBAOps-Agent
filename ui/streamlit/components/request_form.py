from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st


def build_request() -> dict:
    now = datetime.now(timezone.utc)
    default_start = now - timedelta(hours=1)

    mode = st.radio(
        "분석 모드",
        options=["fast", "swarm", "hybrid"],
        format_func=lambda v: {
            "fast":   "⚡ Fast (정해진 그래프, 30s)",
            "swarm":  "🐝 Swarm (specialist 자율, 60~180s)",
            "hybrid": "🔬 Hybrid (Fast → Swarm Deep dive)",
        }[v],
        index=0,
        horizontal=True,
        help=(
            "Fast: router→OS/DB/Log subgraph→hypothesis→reporter 정해진 흐름. 30s 안팎.\n"
            "Swarm: 4 specialist (OS/DB/Log/Query) ReAct + 자율 핸드오프.\n"
            "Hybrid: 먼저 Fast 로 1차 분석 → 그 결과를 Swarm 에 컨텍스트로 주입해 follow-up."
        ),
    )
    lens = st.selectbox("분석 lens", ["multi", "os", "db", "log", "query"], index=0)
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
