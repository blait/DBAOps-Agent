"""Incident Story 뷰 — Sentry/Linear 스타일.

최상위 hypothesis 를 hero card 로, 그 근거 finding 들을 inline 으로,
그 다음 다른 hypotheses 와 unrelated findings 를 차례로.
"""

from __future__ import annotations

import streamlit as st

from ._common import (
    DOMAIN_ICON,
    SEV_BADGE,
    SEV_RANK,
    conf_bar,
    find_by_id,
    render_evidence_block,
)


def _render_finding_card(f: dict) -> None:
    sev = f.get("severity", "info")
    badge = SEV_BADGE.get(sev, "•")
    icon = DOMAIN_ICON.get(f.get("domain", "?"), "•")
    with st.container(border=True):
        st.markdown(
            f"**{badge} `[{sev.upper()}]` {icon} {f.get('title','')}**  \n"
            f"<span style='color:#888'>domain={f.get('domain','?')} · id=`{f.get('id','')}` · {f.get('timestamp','—')}</span>",
            unsafe_allow_html=True,
        )
        with st.expander("evidence", expanded=False):
            render_evidence_block(st, f.get("evidence") or [])


def render(report: dict) -> None:
    findings = report.get("findings") or []
    hypotheses = report.get("hypotheses") or []
    request = report.get("request") or {}
    trace = report.get("trace") or []

    # ── 헤더 ──
    tr = request.get("time_range") or {}
    header_cols = st.columns([3, 1, 1, 1])
    header_cols[0].markdown(
        f"**lens=`{request.get('lens','?')}`** · **window** `{tr.get('start','?')[:19]}` → `{tr.get('end','?')[:19]}`  \n"
        f"target: {', '.join(request.get('targets') or []) or '—'}"
    )
    header_cols[1].metric("findings", len(findings))
    header_cols[2].metric("hypotheses", len(hypotheses))
    total_ms = sum(ev.get("duration_ms", 0) or 0 for ev in trace)
    header_cols[3].metric("analysis", f"{total_ms/1000:.1f}s")

    if not hypotheses:
        st.warning("교차 도메인 가설이 만들어지지 않았습니다. 아래 finding 만 표시합니다.")
    else:
        sorted_hyp = sorted(hypotheses, key=lambda h: -(h.get("confidence", 0.0) or 0.0))
        top = sorted_hyp[0]
        rest = sorted_hyp[1:]

        # ── HERO: 최상위 가설 ──
        st.divider()
        conf = top.get("confidence", 0.0) or 0.0
        st.markdown(f"### 🎯 Top Hypothesis")
        with st.container(border=True):
            st.markdown(
                f"#### {top.get('statement','')}"
            )
            cc = st.columns([1, 5])
            cc[0].markdown(f"**confidence**  \n`{conf_bar(conf)}` **{conf:.2f}**")
            ref_ids = top.get("supporting_finding_ids") or []
            cc[1].markdown(f"**근거 finding ({len(ref_ids)}건)**")

            for fid in ref_ids:
                f = find_by_id(findings, fid)
                if f:
                    _render_finding_card(f)
                else:
                    st.caption(f"- (id={fid} 누락)")

        # ── 그 외 가설 ──
        if rest:
            st.markdown("### Other Hypotheses")
            for h in rest:
                c = h.get("confidence", 0.0) or 0.0
                with st.container(border=True):
                    st.markdown(
                        f"`{conf_bar(c)}` **{c:.2f}** — {h.get('statement','')}"
                    )
                    refs = h.get("supporting_finding_ids") or []
                    if refs:
                        with st.expander(f"근거 {len(refs)}건"):
                            for fid in refs:
                                f = find_by_id(findings, fid)
                                if f:
                                    _render_finding_card(f)

    # ── 가설에 안 묶인 finding ──
    referenced = {fid for h in hypotheses for fid in (h.get("supporting_finding_ids") or [])}
    orphans = [f for f in findings if f.get("id") not in referenced]
    if orphans:
        st.divider()
        st.markdown(f"### 📋 Other Findings ({len(orphans)})")
        orphans.sort(key=lambda f: (SEV_RANK.get(f.get("severity", "info"), 9), f.get("domain", "z")))
        for f in orphans:
            _render_finding_card(f)

    # ── Next actions ──
    if report.get("next_actions"):
        st.divider()
        st.markdown("### ✅ Next Actions")
        for a in report["next_actions"]:
            st.markdown(f"- {a}")
