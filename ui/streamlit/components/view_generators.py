"""시나리오 생성기 라이브 모니터 — task 진행 카드 + CloudWatch Logs tail."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import streamlit as st

import ecs_client


_STATUS_BADGE = {
    "PROVISIONING": "🟨",
    "PENDING":      "🟨",
    "ACTIVATING":   "🟨",
    "RUNNING":      "🟩",
    "DEACTIVATING": "🟧",
    "STOPPING":     "🟧",
    "DEPROVISIONING": "🟧",
    "STOPPED":      "⬛",
}


def _status_chip(status: str | None) -> str:
    if not status:
        return "❔ unknown"
    return f"{_STATUS_BADGE.get(status, '•')} `{status}`"


def _refresh_log_window(group: str, stream: str, key: str, max_lines: int = 300) -> None:
    """session_state 의 log buffer 를 업데이트하고 화면에 그린다."""
    state_key = f"loglines:{key}"
    token_key = f"logtoken:{key}"
    lines: list[dict] = st.session_state.get(state_key, [])
    next_token = st.session_state.get(token_key)

    # 한 번 호출 — 새 chunk
    out = ecs_client.tail_log_events(group, stream, next_token=next_token, limit=200)
    new_events = out.get("events") or []
    if new_events:
        lines.extend(new_events)
        # 끝에서 max_lines 만 유지
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        st.session_state[state_key] = lines
    if out.get("next_token"):
        st.session_state[token_key] = out["next_token"]

    if not lines:
        st.caption("(아직 로그 없음)")
        return
    text = "\n".join(f"{(ev.get('ts') or '')[:19]}  {ev.get('message','')}" for ev in lines[-max_lines:])
    st.code(text, language="text", wrap_lines=False)


def _render_task_card(task_id: str) -> None:
    info = ecs_client.describe_task(task_id)
    if not info:
        st.warning(f"task `{task_id}` describe 결과 없음 (이미 사라졌거나 권한 문제).")
        if st.button("🗑 추적 해제", key=f"untrack-{task_id}"):
            tracked = st.session_state.get("tracked_tasks") or []
            st.session_state["tracked_tasks"] = [t for t in tracked if t != task_id]
            st.rerun()
        return

    status = info.get("last_status")
    container = info.get("container_status")
    family = info.get("family")
    stopped_reason = info.get("stopped_reason")

    with st.container(border=True):
        cols = st.columns([2, 1, 1, 1])
        cols[0].markdown(f"### {family}")
        cols[0].caption(f"task `{task_id[:12]}…`")
        cols[1].metric("task", _status_chip(status), label_visibility="collapsed")
        cols[1].caption("task")
        cols[2].metric("container", _status_chip(container), label_visibility="collapsed")
        cols[2].caption("container")

        with cols[3]:
            if status not in {"STOPPED", None}:
                if st.button("⏹ 중지", key=f"stop-{task_id}", use_container_width=True):
                    try:
                        ecs_client.stop_task(task_id)
                        st.toast("stop_task 호출됨")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"stop error: {e}")
            else:
                if st.button("🗑 추적 해제", key=f"untrack-{task_id}", use_container_width=True):
                    tracked = st.session_state.get("tracked_tasks") or []
                    st.session_state["tracked_tasks"] = [t for t in tracked if t != task_id]
                    # 로그 버퍼도 정리
                    for k in list(st.session_state.keys()):
                        if k.endswith(f":{task_id}"):
                            del st.session_state[k]
                    st.rerun()

        # timing
        meta_cols = st.columns(4)
        meta_cols[0].caption(f"created  \n`{info.get('created_at') or '—'}`")
        meta_cols[1].caption(f"started  \n`{info.get('started_at') or '—'}`")
        meta_cols[2].caption(f"stopped  \n`{info.get('stopped_at') or '—'}`")
        ec = info.get("exit_code")
        meta_cols[3].caption(f"exit code  \n`{ec if ec is not None else '—'}`")

        if stopped_reason:
            st.error(f"stopped: {stopped_reason}")

        # 로그
        log_group = info.get("log_group")
        log_stream = info.get("log_stream")
        if log_group and log_stream:
            with st.expander(f"📜 CloudWatch Logs — `{log_group}` / `{log_stream[-60:]}`", expanded=True):
                _refresh_log_window(log_group, log_stream, key=task_id)
        else:
            st.caption("로그 stream 정보 없음 (task definition logConfiguration 확인).")


def _render_trigger_panel() -> None:
    """시나리오 트리거 버튼 패널 — Generators 탭 안에 배치."""
    subnets = ecs_client.default_subnets()
    sgs = ecs_client.default_security_groups()
    if not subnets:
        st.warning("ECS_SUBNETS 환경변수 비어있음. 트리거가 비활성화됩니다.")

    st.markdown("#### ▶ 시나리오 트리거")
    st.caption(
        "버튼 클릭 시 ECS Fargate Spot 으로 task 1개를 즉시 띄우고, "
        "아래 라이브 모니터에 자동 등록됩니다."
    )
    cols = st.columns(2)
    for i, sc in enumerate(ecs_client.SCENARIOS):
        with cols[i % 2]:
            if st.button(sc["label"], key=f"scn-{sc['key']}",
                         use_container_width=True, disabled=not subnets):
                try:
                    res = ecs_client.trigger_scenario(
                        sc["key"], subnets=subnets, security_groups=sgs or None
                    )
                    if res.get("ok"):
                        # 자동 추적 등록
                        tasks = st.session_state.get("tracked_tasks") or []
                        if res["task_id"] not in tasks:
                            tasks.append(res["task_id"])
                            st.session_state["tracked_tasks"] = tasks
                        st.toast(
                            f"started {res['family']} · task {res['task_id'][:10]}",
                            icon="▶",
                        )
                        st.rerun()
                    else:
                        st.error(f"failed: {res.get('failures')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"RunTask error: {e}")


def render(autorefresh_sec: int = 5) -> None:
    """Generators 탭 메인."""
    tracked: list[str] = st.session_state.get("tracked_tasks") or []

    # ── 트리거 패널 (탭 상단) ──
    _render_trigger_panel()

    st.divider()

    # ── 라이브 모니터 헤더 ──
    head_cols = st.columns([3, 1, 1])
    head_cols[0].markdown("#### 📺 라이브 모니터")
    auto = head_cols[1].toggle("자동 새로고침", value=True, key="gen-auto")
    if head_cols[2].button("🔄 새로고침", use_container_width=True):
        st.rerun()
    st.caption(
        "트리거된 task 는 자동 등록됩니다. STOPPED 가 되어도 로그/상태는 남고, "
        "카드의 🗑 버튼으로 추적 해제할 수 있습니다."
    )

    if not tracked:
        st.info("추적 중인 task 없음. 위 시나리오 버튼으로 시작하세요.")

    # 사용자가 직접 task_id 추적 (수동 외부 trigger 케이스)
    with st.expander("➕ 다른 task_id 직접 추적"):
        manual = st.text_input("task_id", key="manual-track-input", placeholder="ECS task UUID")
        if st.button("추적 시작", key="manual-track-btn") and manual.strip():
            tasks = list(tracked)
            if manual.strip() not in tasks:
                tasks.append(manual.strip())
            st.session_state["tracked_tasks"] = tasks
            st.session_state["manual-track-input"] = ""
            st.rerun()

    # 추적 카드들
    for tid in tracked:
        _render_task_card(tid)

    # 그 외 RUNNING task 도 보여주기 (정보용)
    st.divider()
    st.markdown("#### 그 외 RUNNING task")
    try:
        running = ecs_client.list_running_tasks()
    except Exception as e:  # noqa: BLE001
        st.error(f"describe_tasks error: {e}")
        running = []
    others = [r for r in running if r.get("task_id") not in set(tracked)]
    if others:
        st.dataframe(others, use_container_width=True, hide_index=True)
        st.caption("👆 위 목록의 task_id 를 복사해 위 expander 로 추적할 수 있습니다.")
    else:
        st.caption("그 외 실행 중인 task 없음.")

    # 자동 새로고침 — 추적 중인 task 가 1개라도 있을 때만
    if auto and tracked:
        # 모두 STOPPED 면 폴링 중단
        any_active = any(
            (ecs_client.describe_task(tid) or {}).get("last_status") not in {"STOPPED", None}
            for tid in tracked
        )
        if any_active:
            time.sleep(autorefresh_sec)
            st.rerun()