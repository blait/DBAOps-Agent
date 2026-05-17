"""DBAOps-Agent Streamlit chat UI — fast/swarm/hybrid + 멀티턴 + 시나리오 라이브 모니터."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import streamlit as st

import ecs_client
from agentcore_client import invoke_stream as agentcore_invoke_stream
from components import view_fast_stream, view_generators, view_swarm
from components.view_fast_stream import _evidence_chip

st.set_page_config(page_title="DBAOps-Agent", layout="wide")
st.title("DBAOps-Agent")
st.caption("LangGraph + AgentCore — OS / DB / Log / Query 분석 (chat) + 시나리오 라이브 모니터")

# ───────────────────────── 세션 상태 ─────────────────────────
if "history" not in st.session_state:
    st.session_state["history"] = []
if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())[:8]
if "tracked_tasks" not in st.session_state:
    st.session_state["tracked_tasks"] = []   # list[str]: 추적 중인 ECS task_id


def _track_task(task_id: str) -> None:
    tasks = st.session_state.get("tracked_tasks") or []
    if task_id not in tasks:
        tasks.append(task_id)
    st.session_state["tracked_tasks"] = tasks


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
    st.caption("🧪 시나리오 트리거는 **시나리오 라이브 모니터** 탭으로 이동했습니다.")


# ───────────────────────── 메시지 히스토리 헬퍼 ─────────────────────────
def _build_fast_context_from_history() -> dict:
    if not st.session_state["history"]:
        return {}
    last = st.session_state["history"][-1]
    rep = last.get("report") or {}
    sw = last.get("swarm") or {}

    findings = list(rep.get("findings") or [])
    hypotheses = list(rep.get("hypotheses") or [])
    next_actions = list(rep.get("next_actions") or [])

    if not findings and sw.get("messages"):
        for m in reversed(sw["messages"]):
            if m.get("role") == "ai" and not (m.get("tool_calls") or []) and (m.get("text") or "").strip():
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


# ───────────────────────── 메인 탭 ─────────────────────────
tab_chat, tab_gen = st.tabs(["💬 분석 채팅", "🧪 시나리오 라이브 모니터"])


# ── 채팅 탭 ──
with tab_chat:
    # 히스토리 렌더
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
                        for ev in (f.get("evidence") or [])[:2]:
                            chip = _evidence_chip(ev)
                            if chip:
                                st.caption("　└ " + chip)
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

    # 시나리오 카드의 "💬 채팅에 보내기" 가 채워둔 prefill 이 있으면 보여 주기
    prefill = st.session_state.pop("chat_prefill", None)
    if prefill:
        st.info(
            f"📋 시나리오에서 가져온 추천 prompt 가 준비됐습니다 (lens=`{prefill.get('lens','?')}`).  \n"
            f"`{prefill.get('free_text','')}`  \n"
            f"채팅 입력창에 붙여 넣고 Enter 만 치면 분석이 시작됩니다."
        )
        # session_state 에 한 번 더 보존 — 사용자가 즉시 누를 수 있도록
        st.session_state["chat_prefill_pending"] = prefill

    prompt = st.chat_input("분석할 자연어 요청을 입력하세요 (예: Aurora 락 경합 분석)")

    if prompt:
        if not runtime_arn:
            st.warning("AGENTCORE_RUNTIME_ARN 이 비어있어 호출할 수 없습니다.")
            st.stop()

        # 추천 prompt 사용 시 lens override (사용자가 직접 바꿨을 수도 있어 저장된 prefill 의 lens 가 우선)
        active_lens = lens
        pending = st.session_state.pop("chat_prefill_pending", None)
        if pending and pending.get("free_text") == prompt and pending.get("lens"):
            active_lens = pending["lens"]

        base_request: dict = {
            "mode": mode,
            "lens": active_lens,
            "time_range": {"start": start, "end": end},
            "targets": [t.strip() for t in targets.split(",") if t.strip()],
            "free_text": prompt,
            "session_id": st.session_state["session_id"],
        }

        with st.chat_message("user", avatar="🙋"):
            st.markdown(prompt)
            st.caption(
                f"mode=`{mode}` · lens=`{active_lens}` · "
                f"window {start[:19]} → {end[:19]} · "
                f"targets: {', '.join(base_request['targets']) or '—'}"
            )

        turn: dict = {
            "free_text":   prompt,
            "mode":        mode,
            "lens":        active_lens,
            "start":       start,
            "end":         end,
            "targets":     base_request["targets"],
            "report":      None,
            "swarm":       None,
            "elapsed":     None,
            "fast_context": None,
        }

        live = st.container(border=True)
        with live:
            st.markdown("**🤖 분석 진행 중…**")
            t0_total = datetime.now(timezone.utc)

            fast_report: dict = {}
            if mode in ("fast", "hybrid"):
                st.markdown("**⚡ Fast 분석**")
                fast_req = {**base_request, "mode": "fast"}
                fast_report = view_fast_stream.render_stream(agentcore_invoke_stream(fast_req)) or {}
                turn["report"] = fast_report

            if mode in ("swarm", "hybrid"):
                st.markdown("**🐝 Swarm 분석**")
                swarm_req = {**base_request, "mode": "swarm"}

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

        st.session_state["history"].append(turn)
        st.rerun()


# ── 시나리오 라이브 모니터 탭 ──
with tab_gen:
    view_generators.render(autorefresh_sec=int(os.environ.get("GEN_REFRESH_SEC", "5")))
