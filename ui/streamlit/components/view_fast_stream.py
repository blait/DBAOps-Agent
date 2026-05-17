"""Fast streaming 진행 상황 + 최종 리포트 카드."""

from __future__ import annotations

from typing import Any, Iterator

import streamlit as st


def _evidence_chip(ev: Any) -> str | None:
    """evidence 한 항목을 한 줄 chip 으로 변환 — 도구 + 메트릭 + 수치 + 시점 노출."""
    if isinstance(ev, dict):
        # OS / DB / log summary 들이 첫 항목으로 넣는 표준 dict
        tool = ev.get("tool")
        metric = ev.get("metric") or ev.get("query_or_metric") or ev.get("template")
        ts = ev.get("ts")
        value = ev.get("value")
        z = ev.get("z")
        n_rows = ev.get("n_rows")
        count = ev.get("count")
        ratio = ev.get("ratio_or_total")
        summary = ev.get("summary")
        source = ev.get("source")

        bits: list[str] = []
        if tool:
            bits.append(f"🛠 `{tool}`")
        if source:
            bits.append(f"src=`{source}`")
        if metric:
            bits.append(f"metric=`{str(metric)[:60]}`")
        if value is not None:
            bits.append(f"value=`{value}`")
        if z is not None:
            bits.append(f"z=`{z}`")
        if n_rows is not None:
            bits.append(f"rows=`{n_rows}`")
        if count is not None:
            bits.append(f"count=`{count}`")
        if ratio:
            bits.append(f"ratio=`{ratio}`")
        if ts:
            bits.append(f"@`{str(ts)[:19]}`")
        if summary:
            bits.append(f"— {summary}")

        if not bits:
            return None
        return " · ".join(bits)
    if isinstance(ev, str):
        s = ev.strip()
        return s[:200] if s else None
    return None


_NODE_LABEL = {
    "router":         "🧭 Router",
    "os_subgraph":    "🖥️ OS subgraph",
    "db_subgraph":    "🗄️ DB subgraph",
    "log_subgraph":   "📜 Log subgraph",
    "hypothesis":     "💡 Hypothesis",
    "reporter":       "📤 Reporter",
}


def render_stream(events: Iterator[dict]) -> dict:
    """fast iter_fast() ndjson 이벤트를 받아 진행 상황을 chat 형태로 표시,
    최종 report dict 반환 (없으면 빈 dict).
    """
    progress_box = st.empty()
    log_lines: list[str] = []

    def _refresh():
        progress_box.markdown("\n".join(log_lines) if log_lines else "_(시작 대기)_")

    report: dict = {}
    err: str | None = None

    for ev in events:
        t = ev.get("type")
        if t == "start":
            log_lines.append("▶ 분석 시작")
            _refresh()
        elif t == "node":
            label = _NODE_LABEL.get(ev.get("node"), ev.get("node") or "?")
            log_lines.append(f"• {label} — {ev.get('summary','')}")
            _refresh()
        elif t == "report":
            report = ev.get("report") or {}
        elif t == "done":
            log_lines.append("✅ 완료")
            _refresh()
        elif t == "error":
            err = ev.get("error")
            log_lines.append(f"❌ {err}")
            _refresh()
            break

    if report:
        with st.container(border=True):
            findings = report.get("findings") or []
            hypotheses = report.get("hypotheses") or []
            cnt_e = sum(1 for f in findings if f.get("severity") == "error")
            cnt_w = sum(1 for f in findings if f.get("severity") == "warn")
            cnt_i = sum(1 for f in findings if f.get("severity") == "info")
            st.markdown(
                f"#### 📋 1차 분석 결과 — finding **{len(findings)}**건 "
                f"(🟥 {cnt_e} · 🟧 {cnt_w} · 🟦 {cnt_i}) · hypothesis **{len(hypotheses)}**건"
            )
            for f in findings[:10]:
                sev = (f.get("severity") or "info").upper()
                badge = {"ERROR": "🟥", "WARN": "🟧", "INFO": "🟦"}.get(sev, "•")
                st.markdown(f"- {badge} `[{sev}]` `{f.get('domain','?')}` · {f.get('title','')}")
                # 도구·수치 chip — evidence 첫 1~2 항목을 그대로 노출
                ev_list = f.get("evidence") or []
                shown = 0
                for ev in ev_list[:2]:
                    chip = _evidence_chip(ev)
                    if chip:
                        st.caption("　└ " + chip)
                        shown += 1
                if not shown and ev_list:
                    # evidence 가 비표준 형태면 raw 첫 항목을 그대로
                    raw = ev_list[0]
                    if isinstance(raw, (dict, list)):
                        st.caption("　└ " + str(raw)[:200])
            if len(findings) > 10:
                st.caption(f"...외 {len(findings)-10}건")
            if hypotheses:
                st.markdown("**가설 (hypotheses)**")
                for h in hypotheses[:5]:
                    c = h.get("confidence", 0.0) or 0.0
                    st.markdown(f"- conf {c:.2f} — {h.get('statement','')}")

    if err and not report:
        st.error(err)

    return report
