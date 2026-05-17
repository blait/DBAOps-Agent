"""DBAOps-Agent Streamlit chat UI — fast/swarm/hybrid 모드 + 멀티턴 대화."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import streamlit as st

import ecs_client
from agentcore_client import invoke_stream as agentcore_invoke_stream
from components import view_fast_stream, view_swarm

st.set_page_config(page_title="DBAOps-Agent", layout="wide")
st.title("DBAOps-Agent")
st.caption("LangGraph + AgentCore — OS / DB / Log / Query 분석 (chat)")

# ───────────────────────── 세션 상태 ─────────────────────────
if "history" not in st.session_state:
    st.session_state["history"] = []   # list[dict]: turn 별 입출력 모음
if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())[:8]


# ───────────────────────── Sidebar ─────────────────────────
with st.sidebar:
    st.markdown("### 분석 옵션")
    mode = st.radio(
        "모드",
        options=["fast", "swarm", "hybrid"],
        format_func=lambda v: {
            "fast":   "⚡ Fast (정해진 그래프)",
            "swarm":  "🐝 Swarm (specialist 자율)",
            "hybrid": "🔬 Hybrid (Fast→Swarm)",
        }[v],
        index=0,
        horizontal=False,
    )
    lens = st.selectbox("lens", ["multi", "os", "db", "log", "query"], index=0)
    now = datetime.now(timezone.utc)
    default_start = now - timedelta(hours=1)
    start = st.text_input("Start (UTC)", default_start.isoformat(timespec="seconds"))
    end = st.text_input("End (UTC)", now.isoformat(timespec="seconds"))
    targets = st.text_input("대상 (콤마 구분)", "ec2-prometheus")

    st.divider()
    use_prev_context = st.toggle(
        "이전 답변을 다음 요청에 컨텍스트로 사용",
        value=True,
        help="이전 turn 의 findings/hypotheses 를 swarm/hybrid 의 fast_context 로 자동 주입.",
    )

    st.divider()
    if st.button("🗑 대화 초기화", use_container_width=True):
        st.session_state["history"] = []
        st.session_state["session_id"] = str(uuid.uuid4())[:8]
        st.rerun()

    runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
    st.caption(f"runtime: `{runtime_arn.rsplit('/',1)[-1] or '(unset)'}`")
    st.caption(f"session: `{st.session_state['session_id']}`")

    st.divider()
    with st.expander("🧪 시나리오 트리거 (생성기)"):
        subnets = ecs_client.default_subnets()
        sgs = ecs_client.default_security_groups()
        if not subnets:
            st.warning("ECS_SUBNETS 환경변수 비어있음")
        for sc in ecs_client.SCENARIOS:
            if st.button(sc["label"], key=f"scn-{sc['key']}", use_container_width=True, disabled=not subnets):
                try:
                    res = ecs_client.trigger_scenario(sc["key"], subnets=subnets, security_groups=sgs or None)
                    if res.get("ok"):
                        st.success(f"started `{res['family']}` task `{res['task_id'][:8]}…`")
                    else:
                        st.error(f"failed: {res.get('failures')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"RunTask error: {e}")
        if st.button("🔄 RUNNING task 새로고침", use_container_width=True):
            st.rerun()
        try:
            running = ecs_client.list_running_tasks()
            if running:
                st.caption("**RUNNING**")
                for r in running:
                    st.caption(f"- {r['family']} · {r['last_status']} · {r['task_id'][:10]}")
        except Exception as e:  # noqa: BLE001
            st.caption(f"describe error: {e}")


# ───────────────────────── 메시지 히스토리 렌더 ─────────────────────────
def _build_fast_context_from_history() -> dict:
    """직전 turn 의 fast/swarm 결과에서 fast_context 조립."""
    if not st.session_state["history"]:
        return {}
    last = st.session_state["history"][-1]
    rep = last.get("report") or {}
    sw = last.get("swarm") or {}

    findings = list(rep.get("findings") or [])
    hypotheses = list(rep.get("hypotheses") or [])
    next_actions = list(rep.get("next_actions") or [])

    # swarm 결과의 마지막 ai 메시지에서 finding 식 한국어 정리를 추가 텍스트로 이어붙이기는 생략 —
    # report 가 있으면 그걸 우선 사용. 없을 때만 swarm message 마지막 정리를 free-text 로.
    if not findings and sw.get("messages"):
        for m in reversed(sw["messages"]):
            if m.get("role") == "ai" and not (m.get("tool_calls") or []) and (m.get("text") or "").strip():
                # findings 형식이 아니므로 hypothesis 처럼 한 건 추가
                hypotheses.append({
                    "confidence": 0.5,
                    "statement": (m.get("text") or "")[:1500],
                    "supporting_finding_ids": [],
                })
                break

    return {
        "findings": findings[:30],
        "hypotheses": hypotheses[:10],
        "next_actions": next_actions[:10],
    }


def _summarize_turn(turn: dict) -> str:
    """assistant 풍선 안에 표시할 짧은 한 줄 요약."""
    rep = turn.get("report") or {}
    sw = turn.get("swarm") or {}
    bits: list[str] = []
    bits.append(f"`{turn.get('mode','?')}`")
    if rep:
        f = len(rep.get("findings") or [])
        h = len(rep.get("hypotheses") or [])
        bits.append(f"fast: finding {f} · hypothesis {h}")
    if sw and sw.get("messages"):
        bits.append(f"swarm: msg {len(sw['messages'])} · handoff {max(0, len(sw.get('handoffs') or []) - 1)}")
    elapsed = turn.get("elapsed")
    if elapsed:
        bits.append(f"⏱ {elapsed:.1f}s")
    return " · ".join(bits)


# 페이지 본문 — 누적된 turn 들 렌더
for turn in st.session_state["history"]:
    with st.chat_message("user", avatar="🙋"):
        st.markdown(turn.get("free_text") or "_(empty)_")
        st.caption(
            f"mode=`{turn.get('mode','?')}` · lens=`{turn.get('lens','?')}` · "
            f"window {turn.get('start','?')[:19]} → {turn.get('end','?')[:19]} · "
            f"targets: {', '.join(turn.get('targets') or []) or '—'}"
        )

    with st.chat_message("assistant", avatar="🤖"):
        st.caption(_summarize_turn(turn))
        rep = turn.get("report") or {}
        if rep:
            with st.expander("📋 1차 (Fast) 결과", expanded=False):
                findings = rep.get("findings") or []
                for f in findings[:15]:
                    sev = (f.get("severity") or "info").upper()
                    badge = {"ERROR": "🟥", "WARN": "🟧", "INFO": "🟦"}.get(sev, "•")
                    st.markdown(f"- {badge} `[{sev}]` `{f.get('domain','?')}` · {f.get('title','')}")
                hyps = rep.get("hypotheses") or []
                if hyps:
                    st.markdown("**가설**")
                    for h in hyps[:5]:
                        c = h.get("confidence", 0.0) or 0.0
                        st.markdown(f"- conf {c:.2f} — {h.get('statement','')}")

        sw = turn.get("swarm") or {}
        if sw:
            with st.expander("🐝 Swarm 대화 / 최종 정리", expanded=bool(sw and not rep)):
                view_swarm.render(sw, request={
                    "lens":       turn.get("lens"),
                    "targets":    turn.get("targets"),
                    "free_text":  turn.get("free_text"),
                    "time_range": {"start": turn.get("start"), "end": turn.get("end")},
                })


# ───────────────────────── 신규 chat 입력 ─────────────────────────
prompt = st.chat_input("분석할 자연어 요청을 입력하세요 (예: Aurora 락 경합 분석)")

if prompt:
    if not runtime_arn:
        st.warning("AGENTCORE_RUNTIME_ARN 이 비어있어 호출할 수 없습니다.")
        st.stop()

    base_request: dict = {
        "mode": mode,
        "lens": lens,
        "time_range": {"start": start, "end": end},
        "targets": [t.strip() for t in targets.split(",") if t.strip()],
        "free_text": prompt,
        "session_id": st.session_state["session_id"],
    }

    # 사용자 풍선 즉시 표시
    with st.chat_message("user", avatar="🙋"):
        st.markdown(prompt)
        st.caption(
            f"mode=`{mode}` · lens=`{lens}` · "
            f"window {start[:19]} → {end[:19]} · "
            f"targets: {', '.join(base_request['targets']) or '—'}"
        )

    # 어시스턴트 결과 영역 — chat_message 컨텍스트 안에서 generator 를 돌리면
    # streamlit 이 with 블록 종료 후에야 push 하므로 streaming 동안 화면이 비어 보인다.
    # 따라서 진행은 일반 컨테이너에서 처리하고, 끝난 후에 chat_message 카드로 요약만 다시 그린다.
    turn: dict = {
        "free_text":   prompt,
        "mode":        mode,
        "lens":        lens,
        "start":       start,
        "end":         end,
        "targets":     base_request["targets"],
        "report":      None,
        "swarm":       None,
        "elapsed":     None,
        "fast_context": None,
    }

    live = st.container(border=True)  # 진행 라이브 — chat_message 밖
    with live:
        st.markdown("**🤖 분석 진행 중…**")
        t0_total = datetime.now(timezone.utc)

        # Fast 단계 (fast 또는 hybrid)
        fast_report: dict = {}
        if mode in ("fast", "hybrid"):
            st.markdown("**⚡ Fast 분석**")
            fast_req = {**base_request, "mode": "fast"}
            fast_report = view_fast_stream.render_stream(agentcore_invoke_stream(fast_req)) or {}
            turn["report"] = fast_report

        # Swarm 단계 (swarm 또는 hybrid)
        if mode in ("swarm", "hybrid"):
            st.markdown("**🐝 Swarm 분석**")
            swarm_req = {**base_request, "mode": "swarm"}

            # fast_context 누적: 이번 턴 fast 결과 + (옵션) 직전 turn 컨텍스트
            ctx: dict = {}
            if fast_report:
                ctx = {
                    "findings": (fast_report.get("findings") or [])[:30],
                    "hypotheses": (fast_report.get("hypotheses") or [])[:10],
                    "next_actions": (fast_report.get("next_actions") or [])[:10],
                }
            if use_prev_context:
                prev = _build_fast_context_from_history()
                merged_findings = (ctx.get("findings") or []) + [f for f in (prev.get("findings") or []) if f]
                merged_hyps = (ctx.get("hypotheses") or []) + [h for h in (prev.get("hypotheses") or []) if h]
                merged_actions = (ctx.get("next_actions") or []) + [a for a in (prev.get("next_actions") or []) if a]
                ctx = {
                    "findings":     merged_findings[:30],
                    "hypotheses":   merged_hyps[:10],
                    "next_actions": merged_actions[:10],
                }
            if ctx and (ctx.get("findings") or ctx.get("hypotheses")):
                swarm_req["fast_context"] = ctx
                turn["fast_context"] = ctx

            sw_final = view_swarm.render_stream(agentcore_invoke_stream(swarm_req), request=swarm_req)
            turn["swarm"] = sw_final

        elapsed = (datetime.now(timezone.utc) - t0_total).total_seconds()
        turn["elapsed"] = elapsed
        st.caption(f"⏱ 총 {elapsed:.1f}s")

    # 히스토리에 저장 — 다음 rerun 에서 chat_message 형태로 자연스럽게 합쳐진다
    st.session_state["history"].append(turn)
    st.rerun()
