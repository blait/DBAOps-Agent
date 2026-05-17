"""Domain Dashboard 뷰 — Grafana 스타일.

OS / DB / Log 3 컬럼 대시보드 + 하단 cross-domain hypotheses.
"""

from __future__ import annotations

import streamlit as st

from ._common import (
    DOMAIN_ICON,
    SEV_BADGE,
    SEV_RANK,
    by_domain,
    conf_bar,
    render_evidence_block,
    severity_counts,
)


def _domain_column(name: str, items: list[dict]) -> None:
    icon = DOMAIN_ICON.get(name, "•")
    cnt = severity_counts(items)
    err, warn, info = cnt.get("error", 0), cnt.get("warn", 0), cnt.get("info", 0)
    st.markdown(f"### {icon} {name.upper()}")
    st.caption(f"🟥 {err} · 🟧 {warn} · 🟦 {info} · total {len(items)}")
    if not items:
        with st.container(border=True):
            st.caption("(이 도메인에서 finding 없음)")
        return

    # severity 우선 정렬은 by_domain 에서 이미 함
    for f in items:
        sev = f.get("severity", "info")
        badge = SEV_BADGE.get(sev, "•")
        with st.container(border=True):
            st.markdown(f"**{badge} {f.get('title','')[:80]}**")
            st.caption(f"`{sev.upper()}` · id=`{f.get('id','')}`")
            with st.expander("evidence"):
                render_evidence_block(st, f.get("evidence") or [])


def render(report: dict) -> None:
    findings = report.get("findings") or []
    hypotheses = report.get("hypotheses") or []
    request = report.get("request") or {}
    trace = report.get("trace") or []

    # ── 상단 컨텍스트 ──
    tr = request.get("time_range") or {}
    header = st.columns([2, 1, 1, 1, 1])
    header[0].markdown(
        f"**lens=`{request.get('lens','?')}`**  \n"
        f"`{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`"
    )
    cnt = severity_counts(findings)
    header[1].metric("🟥", cnt.get("error", 0))
    header[2].metric("🟧", cnt.get("warn", 0))
    header[3].metric("💡", len(hypotheses))
    total_ms = sum(ev.get("duration_ms", 0) or 0 for ev in trace)
    header[4].metric("⏱", f"{total_ms/1000:.1f}s")

    st.divider()

    # ── 3 컬럼 도메인 ──
    grouped = by_domain(findings)
    cols = st.columns(3)
    with cols[0]:
        _domain_column("os", grouped.get("os", []))
    with cols[1]:
        _domain_column("db", grouped.get("db", []))
    with cols[2]:
        _domain_column("log", grouped.get("log", []))

    # ── 하단 cross-domain hypotheses ──
    if hypotheses:
        st.divider()
        st.markdown("### 💡 Cross-domain Hypotheses")
        sorted_hyp = sorted(hypotheses, key=lambda h: -(h.get("confidence", 0.0) or 0.0))
        for h in sorted_hyp:
            c = h.get("confidence", 0.0) or 0.0
            ref_ids = h.get("supporting_finding_ids") or []
            ref_domains = sorted({
                find["domain"] for find in findings
                if find.get("id") in ref_ids and find.get("domain")
            })
            domain_tags = " ".join(f"`{DOMAIN_ICON.get(d,'')} {d}`" for d in ref_domains) or ""
            with st.container(border=True):
                st.markdown(f"**{h.get('statement','')}**")
                cc = st.columns([1, 1, 4])
                cc[0].markdown(f"**conf** `{c:.2f}`")
                cc[1].markdown(conf_bar(c))
                cc[2].markdown(f"**domains** {domain_tags}")
                st.caption(f"refs: {', '.join(ref_ids) or '—'}")

    # ── Next actions ──
    if report.get("next_actions"):
        st.divider()
        st.markdown("### ✅ Next Actions")
        for a in report["next_actions"]:
            st.markdown(f"- {a}")
