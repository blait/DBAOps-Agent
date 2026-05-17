"""Swarm 모드 뷰 — 카드형 메시지 + tool_call/tool_result 매칭 + streaming 실시간 갱신."""

from __future__ import annotations

import json
from typing import Any, Iterator

import streamlit as st


_AGENT_AVATAR = {
    "supervisor":       "🎯",
    "os_specialist":    "🖥️",
    "db_specialist":    "🗄️",
    "log_specialist":   "📜",
    "query_specialist": "🔎",
    "aws_specialist":   "☁️",
}

_ROLE_AVATAR = {
    "human":  "🙋",
    "user":   "🙋",
    "ai":     "🤖",
    "tool":   "🛠️",
    "system": "ℹ️",
}


def _agent_chip(name: str | None) -> str:
    if not name:
        return "🤖 _(unnamed)_"
    icon = _AGENT_AVATAR.get(name, "🤖")
    return f"{icon} `{name}`"


def _is_handoff_tool(name: str | None) -> bool:
    return bool(name) and (name.startswith("transfer_to_") or name.startswith("handoff_to_"))


def _short_args(args: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "…"


def _scalar(v: Any, limit: int = 200) -> str:
    """list/dict 도 한 셀에 담을 수 있게 압축 표현."""
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        s = str(v)
    else:
        try:
            s = json.dumps(v, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


def _render_kv_table(target, kv: dict[str, Any]) -> None:
    """단순 dict 를 key/value 2열 dataframe 으로."""
    if not kv:
        target.caption("_(arguments 없음)_")
        return
    rows = [{"key": k, "value": _scalar(v, limit=400)} for k, v in kv.items()]
    target.dataframe(rows, use_container_width=True, hide_index=True)


def _flatten_for_table(items: list[Any]) -> list[dict] | None:
    """list 안의 원소들을 보고 dict 리스트로 변환 (가능하면)."""
    if not items:
        return None
    if all(isinstance(x, dict) for x in items):
        out: list[dict] = []
        for x in items:
            out.append({k: _scalar(v, limit=200) for k, v in x.items()})
        return out
    # rows 형태: list[list] + columns 별도
    return None


def _render_result_payload(target, obj: Any) -> bool:
    """tool result JSON object 를 적절히 표 형태로 렌더. 표가 됐으면 True."""
    if not isinstance(obj, dict):
        return False

    # 1) sql_readonly: {row_count, columns: [...], rows: [[...]]}
    cols = obj.get("columns")
    rows = obj.get("rows")
    if isinstance(cols, list) and isinstance(rows, list) and rows and isinstance(rows[0], (list, tuple)):
        data = [
            {c: _scalar(v, limit=300) for c, v in zip(cols, r)}
            for r in rows[:200]
        ]
        meta = []
        if obj.get("row_count") is not None:
            meta.append(f"row_count={obj['row_count']}")
        if len(rows) > 200:
            meta.append(f"표시 {len(data)} / {len(rows)}행")
        if meta:
            target.caption(" · ".join(meta))
        target.dataframe(data, use_container_width=True, hide_index=True)
        return True

    # 2) explain plan: {plan: "..."}
    if isinstance(obj.get("plan"), str):
        target.code(obj["plan"], language="text", wrap_lines=False)
        if obj.get("row_count") is not None:
            target.caption(f"row_count={obj['row_count']}")
        return True

    # 3) timeseries: {n_points, series: [{ts, value}, ...]}
    series = obj.get("series")
    if isinstance(series, list) and series and isinstance(series[0], dict) and "ts" in series[0]:
        data = [
            {"ts": _scalar(p.get("ts"), 30), "value": _scalar(p.get("value"))}
            for p in series[:300]
        ]
        if obj.get("n_points") is not None:
            target.caption(f"n_points={obj['n_points']}" + (f" · 표시 {len(data)}" if len(series) > 300 else ""))
        target.dataframe(data, use_container_width=True, hide_index=True)
        return True

    # 4) S3 log fetch: {line_count, lines: [...]}
    lines = obj.get("lines")
    if isinstance(lines, list) and lines and isinstance(lines[0], (str, dict)):
        if isinstance(lines[0], str):
            target.code("\n".join(str(x) for x in lines[:200]), language="text", wrap_lines=False)
            target.caption(f"line_count={obj.get('line_count', len(lines))}"
                           + (" · 잘림" if obj.get("truncated") else ""))
        else:
            data = [{k: _scalar(v, 200) for k, v in (x or {}).items()} for x in lines[:200]]
            target.dataframe(data, use_container_width=True, hide_index=True)
        return True

    # 5) aws-api: 단일 list 키를 가진 dict (db_instances/db_clusters/log_files/alarms/instances/clusters/keys/top_sql/metrics/namespaces)
    list_keys = [k for k, v in obj.items() if isinstance(v, list) and v and isinstance(v[0], (dict, str, int, float))]
    if len(list_keys) == 1:
        key = list_keys[0]
        items = obj[key]
        flat = _flatten_for_table(items)
        if flat:
            count_label = obj.get("count")
            target.caption(f"`{key}`" + (f" · count={count_label}" if count_label is not None else f" · {len(items)}건"))
            target.dataframe(flat, use_container_width=True, hide_index=True)
            return True
        if all(isinstance(x, str) for x in items):
            target.caption(f"`{key}` · {len(items)}건")
            target.dataframe([{key: x} for x in items[:200]], use_container_width=True, hide_index=True)
            return True

    # 6) 다중 list 키 — namespaces + metrics 같이 오는 케이스
    if list_keys and all(isinstance(obj[k], list) for k in list_keys):
        rendered_any = False
        for key in list_keys:
            items = obj[key]
            flat = _flatten_for_table(items)
            if flat:
                target.caption(f"`{key}` · {len(items)}건")
                target.dataframe(flat, use_container_width=True, hide_index=True)
                rendered_any = True
            elif items and all(isinstance(x, str) for x in items):
                target.caption(f"`{key}` · {len(items)}건")
                target.dataframe([{key: x} for x in items[:200]], use_container_width=True, hide_index=True)
                rendered_any = True
        # 스칼라 메타 (count, _truncated, etc) 같이 표시
        scalars = {k: v for k, v in obj.items() if not isinstance(v, list) and not isinstance(v, dict)}
        if scalars:
            _render_kv_table(target, scalars)
        return rendered_any

    return False


def _render_message(m: dict, *, container=None) -> None:
    """한 메시지 카드 렌더. container 가 주어지면 그 안에 (placeholder.container() 등)."""
    target = container if container is not None else st

    role = m.get("role") or "ai"
    name = m.get("name")
    text = m.get("text") or ""
    tool_calls = m.get("tool_calls") or []
    tool_call_id = m.get("tool_call_id")

    # human
    if role in ("human", "user"):
        with target.chat_message("user", avatar="🙋"):
            target.markdown(text or "_(empty)_")
        return

    # tool result
    if role == "tool":
        with target.chat_message("assistant", avatar="🛠️"):
            header = f"🛠️ tool result · `{name or '?'}`"
            if tool_call_id:
                header += f" · id=`{tool_call_id}`"
            target.caption(header)
            obj: Any = None
            if text:
                try:
                    obj = json.loads(text)
                except Exception:
                    obj = None
            rendered = _render_result_payload(target, obj) if isinstance(obj, dict) else False
            if not rendered:
                if isinstance(obj, list):
                    flat = _flatten_for_table(obj)
                    if flat:
                        target.dataframe(flat, use_container_width=True, hide_index=True)
                        rendered = True
                if not rendered and isinstance(obj, dict):
                    _render_kv_table(target, obj)
                    rendered = True
            if not rendered:
                # plain text 또는 JSON 파싱 실패
                target.code(text or "(empty)", language="text", wrap_lines=False)
            # 원본 JSON 은 expander 로 보존
            if obj is not None:
                with target.expander("raw JSON", expanded=False):
                    target.json(obj, expanded=False)
        return

    # ai (specialist)
    avatar = _AGENT_AVATAR.get(name, "🤖")
    with target.chat_message("assistant", avatar=avatar):
        if name:
            target.markdown(f"**{_agent_chip(name)}**")
        if text:
            target.markdown(text)

        for tc in tool_calls:
            tname = tc.get("name") or "?"
            args = tc.get("args") or {}
            if _is_handoff_tool(tname):
                target.markdown(f"➡️ **handoff** · `{tname}`  ·  {_short_args(args)}")
                continue
            target.markdown(f"🛠️ **tool_call** · `{tname}`")
            if isinstance(args, dict):
                _render_kv_table(target, args)
            else:
                target.code(_short_args(args, limit=2000), language="json")


# ───────────────────────── 비스트리밍 (기존 호환) ─────────────────────────


def render(result: dict, request: dict | None = None) -> None:
    """이미 받아둔 swarm 결과(dict) 를 한꺼번에 렌더."""
    if "error" in result:
        st.error(result["error"])
        return

    handoffs = result.get("handoffs") or []
    final = result.get("final_active_agent") or "(unknown)"
    aborted = result.get("aborted")

    cols = st.columns([3, 1, 1, 1])
    if request:
        tr = request.get("time_range") or {}
        cols[0].markdown(
            f"**lens=`{request.get('lens','?')}`** · `{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`  \n"
            f"target: {', '.join(request.get('targets') or []) or '—'}"
        )
    cols[1].metric("핸드오프", max(0, len(handoffs) - 1))
    cols[2].metric("최종 specialist", final.split("_")[0] if "_" in final else final)
    if aborted:
        cols[3].metric("⚠️ 중단", aborted)

    if handoffs:
        st.divider()
        st.markdown("### 🔁 핸드오프 시퀀스")
        st.markdown(" → ".join(_agent_chip(a) for a in handoffs))

    st.divider()
    st.markdown("### 💬 Specialist 대화")
    msgs = result.get("messages") or []
    if not msgs:
        st.info("메시지 없음.")
        return
    for m in msgs:
        _render_message(m)


# ───────────────────────── Streaming ─────────────────────────


def render_stream(events: Iterator[dict], request: dict | None = None) -> dict:
    """invoke_stream() 의 NDJSON 이벤트를 받아 실시간 렌더하고, 누적 결과를 반환.

    반환 dict 는 비스트리밍 render() 의 입력과 동일한 구조 (messages/handoffs/final/aborted).
    """
    # ── 헤더 placeholder ──
    header_box = st.container()
    cols = header_box.columns([3, 1, 1, 1])
    if request:
        tr = request.get("time_range") or {}
        cols[0].markdown(
            f"**lens=`{request.get('lens','?')}`** · `{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`  \n"
            f"target: {', '.join(request.get('targets') or []) or '—'}"
        )
    handoff_metric = cols[1].empty()
    active_metric = cols[2].empty()
    abort_metric = cols[3].empty()

    handoffs: list[str] = []
    handoff_metric.metric("핸드오프", 0)

    # ── 핸드오프 chip 영역 ──
    st.divider()
    st.markdown("### 🔁 핸드오프 시퀀스")
    handoff_chip_box = st.empty()
    handoff_chip_box.caption("(시작 전)")

    st.divider()
    st.markdown("### 💬 Specialist 대화 (실시간)")
    log_box = st.container()  # 메시지가 누적될 컨테이너

    messages: list[dict] = []
    aborted: str | None = None
    final_active: str | None = None
    err: str | None = None

    status_box = st.empty()
    status_box.caption("⏳ 대기 중...")

    n_messages = 0
    for ev in events:
        t = ev.get("type")

        if t == "start":
            entry = ev.get("entry")
            reason = ev.get("reasoning")
            if entry:
                # 첫 active_agent 가 곧 이 entry 로 들어오므로 handoffs 에는 append 하지 않는다 — 중복 방지.
                active_metric.metric(
                    "현재 specialist",
                    entry.split("_")[0] if "_" in entry else entry,
                )
                if reason:
                    status_box.caption(f"▶ 시작 → {_agent_chip(entry)} · {reason}")
                else:
                    status_box.caption(f"▶ 시작 → {_agent_chip(entry)}")
            else:
                status_box.caption("▶ 분석 시작")
        elif t == "handoff":
            agent = ev.get("agent") or "?"
            handoffs.append(agent)
            handoff_metric.metric("핸드오프", max(0, len(handoffs) - 1))
            active_metric.metric(
                "현재 specialist",
                agent.split("_")[0] if "_" in agent else agent,
            )
            handoff_chip_box.markdown(" → ".join(_agent_chip(a) for a in handoffs))
            status_box.caption(f"➡️ 핸드오프 → {_agent_chip(agent)}")
        elif t == "message":
            msg = ev.get("message") or {}
            messages.append(msg)
            n_messages += 1
            with log_box:
                _render_message(msg)
            status_box.caption(f"💬 메시지 {n_messages}건 누적")
        elif t == "abort":
            aborted = ev.get("reason")
            abort_metric.metric("⚠️ 중단", aborted or "abort")
            status_box.warning(f"⚠️ 중단: {aborted}")
        elif t == "error":
            err = ev.get("error")
            status_box.error(f"❌ {err}")
            break
        elif t == "done":
            final_active = ev.get("final_active_agent")
            if final_active:
                active_metric.metric(
                    "최종 specialist",
                    final_active.split("_")[0] if "_" in final_active else final_active,
                )
            status_box.success(f"✅ 완료 · 메시지 {n_messages}건 · 핸드오프 {max(0, len(handoffs) - 1)}회")

            # 최종 정리 카드 — 마지막 ai 메시지가 길고 도구 호출이 없으면 그게 정리
            last_ai = next(
                (m for m in reversed(messages)
                 if m.get("role") == "ai" and not m.get("tool_calls") and (m.get("text") or "").strip()),
                None,
            )
            if last_ai and last_ai.get("text"):
                with log_box:
                    with st.container(border=True):
                        st.markdown("### 📤 최종 정리")
                        st.caption(f"by {_agent_chip(last_ai.get('name'))}")
                        st.markdown(last_ai["text"])

    return {
        "messages": messages,
        "handoffs": handoffs,
        "final_active_agent": final_active,
        "aborted": aborted,
        **({"error": err} if err else {}),
    }
