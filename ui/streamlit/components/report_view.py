from __future__ import annotations

import json

import streamlit as st


_SEV_BADGE = {"error": "🟥", "warn": "🟧", "info": "🟦"}


def _findings_table(findings: list[dict]) -> list[dict]:
    """st.dataframe 용 평탄 표 — evidence 같은 비정형 필드는 제외/요약."""
    rows = []
    for f in findings:
        ev = f.get("evidence") or []
        rows.append(
            {
                "severity": f.get("severity", "info"),
                "domain":   f.get("domain", "?"),
                "id":       f.get("id", ""),
                "title":    f.get("title", ""),
                "evidence_count": len(ev) if isinstance(ev, list) else 1,
                "timestamp": f.get("timestamp", ""),
            }
        )
    return rows


def render_report(report: dict) -> None:
    st.markdown(report.get("markdown", "_(empty)_"))

    findings = report.get("findings") or []
    if findings:
        st.subheader("Findings")
        st.dataframe(_findings_table(findings), use_container_width=True, hide_index=True)

        with st.expander(f"🔍 evidence 상세 ({len(findings)}건)", expanded=False):
            for f in findings:
                sev = f.get("severity", "info")
                badge = _SEV_BADGE.get(sev, "•")
                st.markdown(
                    f"#### {badge} `[{sev.upper()}]` {f.get('title','')}  "
                    f"_(domain={f.get('domain','?')}, id={f.get('id','')})_"
                )
                ev = f.get("evidence") or []
                if not ev:
                    st.caption("(evidence 없음)")
                else:
                    st.code(json.dumps(ev, ensure_ascii=False, indent=2), language="json")
                st.divider()

    hypotheses = report.get("hypotheses") or []
    if hypotheses:
        st.subheader("Hypotheses")
        for h in hypotheses:
            conf = h.get("confidence", 0.0) or 0.0
            refs = ", ".join(h.get("supporting_finding_ids") or [])
            st.markdown(
                f"- **{h.get('statement','')}**  "
                f"_(conf {conf:.2f}; refs: {refs or '—'})_"
            )

    next_actions = report.get("next_actions") or []
    if next_actions:
        st.subheader("Next Actions")
        for a in next_actions:
            st.markdown(f"- {a}")
