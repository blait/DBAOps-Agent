"""DBAOps-Agent Streamlit UI — 단일 페이지: 요청 폼 + 리포트 렌더."""

from __future__ import annotations

import json
import os

import streamlit as st

from components.report_view import render_report
from components.request_form import build_request

st.set_page_config(page_title="DBAOps-Agent", layout="wide")
st.title("DBAOps-Agent")
st.caption("LangGraph + AgentCore — DB / OS / Log 분석")

with st.sidebar:
    st.markdown("### 요청")
    request = build_request()
    submit = st.button("분석 실행", type="primary", use_container_width=True)

if submit:
    runtime_endpoint = os.environ.get("RUNTIME_ENDPOINT", "")
    if not runtime_endpoint:
        st.warning("RUNTIME_ENDPOINT 환경변수가 비어 있어요. Phase 1 끝에 채우세요.")
        st.json(request)
    else:
        with st.spinner("AgentCore Runtime 호출 중..."):
            # TODO: Phase 1에서 boto3 bedrock-agentcore invoke 호출 구현
            report = {"markdown": "(placeholder)", "findings": [], "hypotheses": []}
            render_report(report)

st.divider()
st.code(json.dumps(request, indent=2, ensure_ascii=False), language="json")
