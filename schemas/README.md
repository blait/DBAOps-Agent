# Schemas

JSON Schema 정의. Agent ↔ UI ↔ MCP 간 계약.

- `analysis_request.json` — Streamlit → Runtime 입력
- `analysis_report.json`  — Runtime → Streamlit 출력
- `mcp_tool_io/` — 각 MCP 도구의 I/O 스키마 (각 `mcp_tools/*/tool_io.json` 가 source of truth)
