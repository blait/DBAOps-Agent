"""DBAOps-Agent Streamlit UI — fast 그래프 / swarm streaming + 시나리오 트리거."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import streamlit as st

import ecs_client
from agentcore_client import invoke as agentcore_invoke
from agentcore_client import invoke_stream as agentcore_invoke_stream
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


# ───────────────────────────── 분석 호출 ─────────────────────────────


def _fast_context_from_report(report: dict) -> dict:
    """fast report 에서 swarm 에 넘길 가벼운 dict 만 추출."""
    if not isinstance(report, dict):
        return {}
    return {
        "findings":     report.get("findings") or [],
        "hypotheses":   report.get("hypotheses") or [],
        "next_actions": report.get("next_actions") or [],
    }


if submit:
    if not runtime_arn:
        st.warning("AGENTCORE_RUNTIME_ARN 이 비어있어요.")
        st.stop()
    st.session_state["last_request"] = request
    mode = (request.get("mode") or "fast").lower()

    if mode == "swarm":
        st.session_state["last_result"] = {"swarm_stream_pending": True}
        st.session_state["last_elapsed"] = None
    elif mode == "hybrid":
        # 1단계 — fast 분석 즉시 호출
        t0 = datetime.now(timezone.utc)
        fast_req = {**request, "mode": "fast"}
        with st.spinner("1차 Fast 분석 호출 중..."):
            fast_result = agentcore_invoke(fast_req)
        elapsed_fast = (datetime.now(timezone.utc) - t0).total_seconds()
        # 2단계 — swarm 은 Swarm 탭이 streaming 으로 받음, fast_context 동봉
        fast_ctx = _fast_context_from_report((fast_result or {}).get("report") or {})
        st.session_state["last_result"] = {
            "report": (fast_result or {}).get("report"),
            "swarm_stream_pending": True,
            "fast_context": fast_ctx,
            "fast_elapsed": elapsed_fast,
        }
        st.session_state["last_elapsed"] = elapsed_fast
    else:
        t0 = datetime.now(timezone.utc)
        with st.spinner("AgentCore Runtime 호출 중..."):
            result_obj = agentcore_invoke(request)
        st.session_state["last_result"] = result_obj
        st.session_state["last_elapsed"] = (datetime.now(timezone.utc) - t0).total_seconds()

result = st.session_state.get("last_result")
elapsed = st.session_state.get("last_elapsed")


# ───────────────────────────── Tabs ─────────────────────────────
tab_swarm, tab_triage, tab_story, tab_dash, tab_trace, tab_raw, tab_gen = st.tabs(
    [
        "🐝 Swarm",
        "🚨 Triage",
        "📖 Incident Story",
        "🗂 Domain Dashboard",
        "🧠 Thought Process",
        "🧾 Raw",
        "🧪 Generators",
    ]
)


def _gate_report(tab):
    """fast 모드 결과 (report)가 있으면 반환, 없으면 안내 후 None."""
    if not result:
        tab.info("좌측에서 **분석 실행** 을 눌러 리포트를 받아오세요.")
        return None
    if "error" in result and not result.get("swarm"):
        tab.error(result["error"])
        return None
    rep = result.get("report")
    if not rep:
        tab.info("이번 응답은 swarm 모드입니다. 🐝 Swarm 탭을 보세요.")
        return None
    return rep


# Swarm — streaming 또는 캐시된 결과
with tab_swarm:
    req_cached = st.session_state.get("last_request") or {}
    if not result:
        st.info("swarm/hybrid 모드로 분석 실행하면 여기에 specialist 대화가 실시간 표시됩니다.")
    elif result.get("swarm_stream_pending"):
        # hybrid 라면 fast_context 가 함께 들어있다 → swarm 요청에 포함
        fast_ctx = result.get("fast_context") or {}
        if fast_ctx:
            st.success(
                f"1차 Fast 분석 완료 ({result.get('fast_elapsed', 0):.1f}s) — "
                f"finding {len(fast_ctx.get('findings') or [])}건, "
                f"hypothesis {len(fast_ctx.get('hypotheses') or [])}건. "
                f"이제 Swarm 이 follow-up 을 시작합니다."
            )
            with st.expander("📋 Fast 분석 요약 (swarm 컨텍스트)"):
                if fast_ctx.get("findings"):
                    st.markdown("**findings**")
                    for f in fast_ctx["findings"][:30]:
                        sev = (f.get("severity") or "info").upper()
                        st.markdown(f"- `[{sev}]` `{f.get('domain','?')}` · {f.get('title','')}")
                if fast_ctx.get("hypotheses"):
                    st.markdown("**hypotheses**")
                    for h in fast_ctx["hypotheses"][:10]:
                        c = h.get("confidence", 0.0) or 0.0
                        st.markdown(f"- conf {c:.2f} — {h.get('statement','')}")
        swarm_req = {**req_cached, "mode": "swarm"}
        if fast_ctx:
            swarm_req["fast_context"] = fast_ctx
        t0 = datetime.now(timezone.utc)
        events = agentcore_invoke_stream(swarm_req)
        final = view_swarm.render_stream(events, request=swarm_req)
        elapsed_swarm = (datetime.now(timezone.utc) - t0).total_seconds()
        st.session_state["last_result"] = {
            "swarm": final,
            "request": req_cached,
            "report": result.get("report"),
            "fast_elapsed": result.get("fast_elapsed"),
            "swarm_elapsed": elapsed_swarm,
            "fast_context": fast_ctx,
        }
        st.caption(f"⏱ swarm {elapsed_swarm:.1f}s")
    elif "swarm" in result:
        e = result.get("swarm_elapsed") or elapsed
        if e:
            st.caption(f"⏱ swarm {e:.1f}s")
        view_swarm.render(result["swarm"], request=req_cached)
    elif "error" in result:
        st.error(result["error"])
    else:
        # fast 만 받은 상태 — Deep dive 버튼 제공
        st.info("이번 응답은 fast 모드입니다.")
        if st.button("🔬 Swarm 으로 Deep dive", type="primary", use_container_width=True):
            fast_ctx = _fast_context_from_report(result.get("report") or {})
            st.session_state["last_result"] = {
                "report": result.get("report"),
                "swarm_stream_pending": True,
                "fast_context": fast_ctx,
                "fast_elapsed": elapsed,
            }
            st.rerun()

# Triage
with tab_triage:
    rep = _gate_report(tab_triage)
    if rep is not None:
        if elapsed:
            st.caption(f"⏱ {elapsed:.1f}s")
        view_triage.render(rep)

# Story
with tab_story:
    rep = _gate_report(tab_story)
    if rep is not None:
        view_story.render(rep)

# Dashboard
with tab_dash:
    rep = _gate_report(tab_dash)
    if rep is not None:
        view_dashboard.render(rep)

# Trace
with tab_trace:
    rep = _gate_report(tab_trace)
    if rep is not None:
        view_trace.render(rep)

# Raw
with tab_raw:
    if not result:
        st.info("좌측에서 **분석 실행** 을 눌러 리포트를 받아오세요.")
    elif "error" in result and not result.get("swarm"):
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
