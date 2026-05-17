"""DBAOps-Agent Streamlit UI — 분석 요청 + 부하 생성기 + 추론 과정 가시화."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import streamlit as st

import ecs_client
from agentcore_client import invoke as agentcore_invoke
from components.report_view import render_report
from components.request_form import build_request

st.set_page_config(page_title="DBAOps-Agent", layout="wide")
st.title("DBAOps-Agent")
st.caption("LangGraph + AgentCore — OS / DB / Log 분석 + 시나리오 생성기")

# ───────────────────────────── Sidebar: 요청 ─────────────────────────────
with st.sidebar:
    st.markdown("### 분석 요청")
    request = build_request()
    submit = st.button("분석 실행", type="primary", use_container_width=True)
    runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
    st.caption(f"runtime: `{runtime_arn.rsplit('/',1)[-1] or '(unset)'}`")

# ───────────────────────────── Main: tabs ─────────────────────────────
tab_report, tab_gen = st.tabs(["📊 분석 리포트", "🧪 부하/에러 생성기"])

with tab_report:
    if submit:
        if not runtime_arn:
            st.warning("AGENTCORE_RUNTIME_ARN 이 비어있어요.")
            st.json({"request": request})
        else:
            t0 = datetime.now(timezone.utc)
            with st.spinner("AgentCore Runtime 호출 중..."):
                result = agentcore_invoke(request)
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            st.caption(f"⏱ {elapsed:.1f}s")

            if "error" in result:
                st.error(result["error"])
            else:
                report = result.get("report") or {}
                # Thought process는 reporter가 markdown에 이미 넣어주지만,
                # 별도 expander 로 노드별 timeline 도 시각화한다.
                trace_events = report.get("trace") or []
                if trace_events:
                    with st.expander(f"🧠 Agent thought process ({len(trace_events)} events)", expanded=True):
                        for ev in trace_events:
                            ms = ev.get("duration_ms")
                            tag = f" `{ms}ms`" if ms is not None else ""
                            phase = ev.get("phase", "info")
                            icon = {"enter": "▶", "exit": "■", "warn": "⚠", "error": "✗", "info": "•"}.get(phase, "•")
                            st.markdown(f"{icon} **{ev.get('node','?')}** — {ev.get('summary','')}{tag}")
                            detail = ev.get("detail")
                            if detail:
                                st.json(detail, expanded=False)

                render_report(report)
                with st.expander("raw response"):
                    st.code(json.dumps(result, ensure_ascii=False, indent=2), language="json")
    else:
        st.info("좌측 사이드바에서 요청을 채우고 **분석 실행** 을 누르세요.")
        st.code(json.dumps(request, indent=2, ensure_ascii=False), language="json")

with tab_gen:
    st.markdown("### 시나리오 트리거")
    st.caption(
        "ECS Fargate Spot 으로 부하/에러 생성기 task 를 1회 실행합니다. "
        "EventBridge Scheduler 도 자동 주기로 같은 task 를 띄우므로, 즉시 보고 싶을 때만 사용하세요."
    )

    subnets = ecs_client.default_subnets()
    sgs = ecs_client.default_security_groups()
    if not subnets:
        st.warning("환경변수 `ECS_SUBNETS` 가 비어있어요. (콤마 구분 subnet id)")

    cols = st.columns(2)
    for i, sc in enumerate(ecs_client.SCENARIOS):
        with cols[i % 2]:
            if st.button(sc["label"], key=f"scn-{sc['key']}", use_container_width=True, disabled=not subnets):
                try:
                    res = ecs_client.trigger_scenario(sc["key"], subnets=subnets, security_groups=sgs or None)
                    if res.get("ok"):
                        st.success(f"started `{res['family']}` task `{res['task_id']}`")
                    else:
                        st.error(f"failed: {res.get('failures')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"RunTask error: {e}")

    st.divider()
    cols2 = st.columns([1, 1])
    with cols2[0]:
        if st.button("🔄 새로고침", use_container_width=True):
            st.rerun()
    with cols2[1]:
        st.caption(f"cluster: `{ecs_client.CLUSTER}` · region: `{ecs_client.REGION}`")

    st.markdown("#### 현재 RUNNING task")
    try:
        running = ecs_client.list_running_tasks()
    except Exception as e:  # noqa: BLE001
        st.error(f"describe_tasks error: {e}")
        running = []
    if running:
        st.dataframe(running, use_container_width=True, hide_index=True)
    else:
        st.caption("실행 중인 task 없음.")

    st.markdown("#### 최근 종료된 task (최대 10건)")
    try:
        stopped = ecs_client.list_recent_stopped(10)
    except Exception as e:  # noqa: BLE001
        st.error(f"describe_tasks error: {e}")
        stopped = []
    if stopped:
        st.dataframe(stopped, use_container_width=True, hide_index=True)
    else:
        st.caption("최근 종료된 task 없음.")
