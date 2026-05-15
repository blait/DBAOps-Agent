from __future__ import annotations

import streamlit as st


def render_report(report: dict) -> None:
    st.markdown(report.get("markdown", "_(empty)_"))
    findings = report.get("findings") or []
    if findings:
        st.subheader("Findings")
        st.dataframe(findings)
    hypotheses = report.get("hypotheses") or []
    if hypotheses:
        st.subheader("Hypotheses")
        for h in hypotheses:
            st.markdown(f"- **{h.get('statement', '')}** (conf {h.get('confidence', 0):.2f})")
