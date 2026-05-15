"""DBAOps-Agent Streamlit UI — 단일 페이지: 요청 폼 + 리포트 렌더."""

from __future__ import annotations

import json
import os

import streamlit as st

from agentcore_client import invoke as agentcore_invoke
from components.report_view import render_report
from components.request_form import build_request

st.set_page_config(page_title="DBAOps-Agent", layout="wide")
st.title("DBAOps-Agent")
st.caption("LangGraph + AgentCore — DB / OS / Log 분석")

with st.sidebar:
    st.markdown("### 요청")
    request = build_request()
    submit = st.button("분석 실행", type="primary", use_container_width=True)
    runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
    st.caption(f"runtime: {runtime_arn or '(unset)'}")

if submit:
    if not runtime_arn:
        st.warning("AGENTCORE_RUNTIME_ARN 이 비어있어요. Phase 1 끝에 채우세요.")
        st.json({"request": request})
    else:
        with st.spinner("AgentCore Runtime 호출 중..."):
            result = agentcore_invoke(request)
        if "error" in result:
            st.error(result["error"])
        else:
            render_report(result.get("report") or {})
            with st.expander("raw"):
                st.code(json.dumps(result, ensure_ascii=False, indent=2), language="json")

st.divider()
st.code(json.dumps(request, indent=2, ensure_ascii=False), language="json")
