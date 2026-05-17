"""Triage 뷰 — Datadog/PagerDuty 스타일.

상단 severity 카운트 → 좌측 finding 리스트(필터) → 우측 evidence + linked hypotheses.
"""

from __future__ import annotations

import streamlit as st

from ._common import (
    DOMAIN_ICON,
    SEV_BADGE,
    SEV_RANK,
    conf_bar,
    hypotheses_for,
    render_evidence_block,
    severity_counts,
)


def render(report: dict) -> None:
    findings = report.get("findings") or []
    hypotheses = report.get("hypotheses") or []

    # ── 상단 KPI ──
    cnt = severity_counts(findings)
    cols = st.columns(4)
    cols[0].metric("🟥 Errors",   cnt.get("error", 0))
    cols[1].metric("🟧 Warnings", cnt.get("warn", 0))
    cols[2].metric("🟦 Info",     cnt.get("info", 0))
    cols[3].metric("💡 Hypotheses", f"{len(hypotheses)} / {len(findings)}")

    if not findings:
        st.info("탐지된 finding 없음.")
        return

    st.divider()

    # ── 필터 ──
    fcol1, fcol2 = st.columns([3, 2])
    with fcol1:
        sev_filter = st.multiselect(
            "Severity 필터",
            ["error", "warn", "info"],
            default=["error", "warn", "info"],
            key="triage-sev",
        )
    with fcol2:
        domain_filter = st.multiselect(
            "Domain 필터",
            sorted({f.get("domain", "?") for f in findings}),
            default=sorted({f.get("domain", "?") for f in findings}),
            key="triage-domain",
        )

    filtered = [
        f for f in findings
        if f.get("severity", "info") in sev_filter and f.get("domain", "?") in domain_filter
    ]
    filtered.sort(key=lambda f: (SEV_RANK.get(f.get("severity", "info"), 9), f.get("domain", "z")))

    # ── 본문 좌·우 ──
    left, right = st.columns([2, 3])

    with left:
        st.markdown(f"#### Findings ({len(filtered)})")
        if not filtered:
            st.caption("선택된 필터에 해당하는 finding 없음.")

        # 라디오로 단일 선택
        labels = []
        for f in filtered:
            sev = f.get("severity", "info")
            badge = SEV_BADGE.get(sev, "•")
            icon = DOMAIN_ICON.get(f.get("domain", "?"), "•")
            labels.append(f"{badge} {icon} {f.get('title', '')[:60]}")

        if labels:
            idx = st.radio(
                "선택",
                options=list(range(len(filtered))),
                format_func=lambda i: labels[i],
                label_visibility="collapsed",
                key="triage-pick",
            )
        else:
            idx = None

    with right:
        if filtered and idx is not None:
            f = filtered[idx]
            sev = f.get("severity", "info")
            badge = SEV_BADGE.get(sev, "•")
            icon = DOMAIN_ICON.get(f.get("domain", "?"), "•")
            st.markdown(f"### {badge} `[{sev.upper()}]` {f.get('title', '')}")
            meta_cols = st.columns(3)
            meta_cols[0].caption(f"domain  \n**{icon} {f.get('domain', '?')}**")
            meta_cols[1].caption(f"id  \n`{f.get('id','')}`")
            meta_cols[2].caption(f"timestamp  \n{f.get('timestamp','—')}")

            st.markdown("#### Evidence")
            render_evidence_block(st, f.get("evidence") or [])

            linked = hypotheses_for(findings, hypotheses, f.get("id", ""))
            if linked:
                st.markdown("#### 💡 Linked hypotheses")
                for h in linked:
                    conf = h.get("confidence", 0.0) or 0.0
                    st.markdown(
                        f"- `{conf_bar(conf)}` **{conf:.2f}** — {h.get('statement', '')}"
                    )
        else:
            st.caption("좌측에서 finding 을 선택하세요.")

    # ── 하단 hypotheses ──
    if hypotheses:
        st.divider()
        st.markdown("#### 💡 All Hypotheses (sorted by confidence)")
        sorted_hyp = sorted(hypotheses, key=lambda h: -(h.get("confidence", 0.0) or 0.0))
        for h in sorted_hyp:
            conf = h.get("confidence", 0.0) or 0.0
            refs = ", ".join(h.get("supporting_finding_ids") or [])
            st.markdown(
                f"- `{conf_bar(conf)}` **{conf:.2f}** — {h.get('statement', '')}  \n"
                f"  _refs: {refs or '—'}_"
            )
