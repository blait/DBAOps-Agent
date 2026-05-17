"""DBAOps-Agent Streamlit UI — 분석 / 시나리오 트리거 / 분석 뷰 4종."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import streamlit as st

import ecs_client
from agentcore_client import invoke as agentcore_invoke
from components import view_dashboard, view_story, view_swarm, view_trace, view_triage
from components.request_form import build_request

st.set_page_config(page_title="DBAOps-Agent", layout="wide")
st.title("DBAOps-Agent")
st.caption("LangGraph + AgentCore — OS / DB / Log 분석 + 시나리오 생성기")


# ───────────────────────────── Sidebar ─────────────────────────────
with st.sidebar:
    st.markdown("### 분석 요청")
    request = build_request()
    submit = st.button("분석 실행", type="primary", use_container_width=True)
    runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
    st.caption(f"runtime: `{runtime_arn.rsplit('/',1)[-1] or '(unset)'}`")


# ───────────────────────────── 분석 결과 캐시 ─────────────────────────────
if submit:
    if not runtime_arn:
        st.warning("AGENTCORE_RUNTIME_ARN 이 비어있어요.")
        st.stop()
    t0 = datetime.now(timezone.utc)
    with st.spinner("AgentCore Runtime 호출 중..."):
        result = agentcore_invoke(request)
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    st.session_state["last_result"] = result
    st.session_state["last_elapsed"] = elapsed
    st.session_state["last_request"] = request

result = st.session_state.get("last_result")
elapsed = st.session_state.get("last_elapsed")


# ───────────────────────────── Tabs ─────────────────────────────
tab_triage, tab_story, tab_dash, tab_trace, tab_swarm, tab_raw, tab_gen = st.tabs(
    [
        "🚨 Triage",
        "📖 Incident Story",
        "🗂 Domain Dashboard",
        "🧠 Thought Process",
        "🐝 Swarm",
        "🧾 Raw",
        "🧪 Generators",
    ]
)


def _gate_result(tab):
    """결과가 없으면 안내 후 None 반환."""
    if not result:
        tab.info("좌측에서 **분석 실행** 을 눌러 리포트를 받아오세요.")
        return None
    if "error" in result:
        tab.error(result["error"])
        return None
    return result.get("report") or {}


# Triage
with tab_triage:
    rep = _gate_result(tab_triage)
    if rep is not None:
        if elapsed:
            st.caption(f"⏱ {elapsed:.1f}s")
        view_triage.render(rep)

# Story
with tab_story:
    rep = _gate_result(tab_story)
    if rep is not None:
        view_story.render(rep)

# Dashboard
with tab_dash:
    rep = _gate_result(tab_dash)
    if rep is not None:
        view_dashboard.render(rep)

# Trace
with tab_trace:
    rep = _gate_result(tab_trace)
    if rep is not None:
        view_trace.render(rep)

# Swarm
with tab_swarm:
    if not result:
        st.info("swarm 모드로 분석 실행하면 여기에 핸드오프 시퀀스가 보입니다.")
    elif "error" in result:
        st.error(result["error"])
    elif "swarm" in result:
        view_swarm.render(result["swarm"], request=st.session_state.get("last_request"))
    else:
        st.info("이번 응답은 fast 모드입니다. 사이드바에서 모드를 `swarm` 으로 바꿔 다시 실행해 보세요.")

# Raw
with tab_raw:
    if not result:
        st.info("좌측에서 **분석 실행** 을 눌러 리포트를 받아오세요.")
    elif "error" in result:
        st.error(result["error"])
    else:
        rep = result.get("report") or {}
        if rep:
            with st.expander("Markdown", expanded=False):
                st.markdown(rep.get("markdown", "_(empty)_"))
        with st.expander("Full JSON response"):
            st.code(json.dumps(result, ensure_ascii=False, indent=2), language="json")


# ───────────────────────────── Generators 탭 ─────────────────────────────
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
