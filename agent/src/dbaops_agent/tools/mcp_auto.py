"""MCP `tools/list` 결과를 LangChain StructuredTool 로 자동 변환.

이 모듈의 목적:
- 38개 수동 wrapper (mcp_tools.py) 대신 Gateway 의 tools/list 를 그대로 LLM 에 노출.
- LLM 이 보는 description/inputSchema 는 MCP 서버 자체 것 — 우리가 추가 cheat-sheet 안 박음.
- 우리 PoC 특화 변환 (db_id auto-resolve / EXPLAIN strip / MSK dim wiring) 은 모두
  Lambda handler 에 이미 구현돼 있어 그대로 사용.

응답 정제 (truncate) 만 wrapper 가 한 번 — LLM 컨텍스트 보호.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from .mcp_client import MCPClient

logger = logging.getLogger(__name__)


# JSON Schema → Python type 매핑
_TYPE_MAP = {
    "string":  str,
    "integer": int,
    "number":  float,
    "boolean": bool,
    "array":   list,
    "object":  dict,
}


def _field_type(prop: dict) -> Any:
    """JSON Schema property → Python annotation. anyOf / type 배열은 union 처리."""
    if not isinstance(prop, dict):
        return Any
    t = prop.get("type")
    if isinstance(t, list):
        # ['string', 'null'] 같은 union — 첫 non-null 만 사용
        non_null = [x for x in t if x != "null"]
        return _TYPE_MAP.get(non_null[0], Any) if non_null else Any
    if isinstance(t, str):
        return _TYPE_MAP.get(t, Any)
    if "anyOf" in prop:
        # 첫 element 의 type 만 사용 (최선의 추정)
        for option in prop["anyOf"]:
            if isinstance(option, dict) and option.get("type") and option.get("type") != "null":
                return _TYPE_MAP.get(option["type"], Any)
    return Any


def _make_args_model(name: str, schema: dict) -> type[BaseModel]:
    """JSON Schema (object) → pydantic BaseModel."""
    if not isinstance(schema, dict):
        return create_model(f"{name}Args")  # empty
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for fname, prop in properties.items():
        ftype = _field_type(prop)
        desc = (prop.get("description") if isinstance(prop, dict) else None) or ""
        if fname in required:
            fields[fname] = (ftype, Field(..., description=desc))
        else:
            default = prop.get("default") if isinstance(prop, dict) else None
            # Optional 타입으로 — None 허용
            fields[fname] = (Optional[ftype], Field(default=default, description=desc))
    if not fields:
        return create_model(f"{name}Args")
    return create_model(f"{name}Args", **fields)


def _truncate(obj: Any, max_chars: int = 12000) -> str:
    """LLM 컨텍스트 보호용 응답 직렬화."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    if len(s) > max_chars:
        return s[:max_chars] + f"\n... (truncated, total {len(s)} chars)"
    return s


def _make_invoker(client: MCPClient, full_name: str, max_chars: int):
    """Closure factory — full_name 은 Gateway namespacing 포함된 도구 이름."""
    def _invoke(**kwargs) -> str:
        # None 값은 제거 — 백엔드가 missing/None 둘 다 허용 못 할 수 있음
        args = {k: v for k, v in kwargs.items() if v is not None}
        result = client.call(full_name, args)
        return _truncate(result if result is not None else {}, max_chars=max_chars)
    return _invoke


def _safe_tool_name(full_name: str) -> str:
    """Gateway namespacing 의 '___' 를 LangChain 호환 식별자로 변환.

    LangChain Tool name 은 '^[a-zA-Z0-9_-]{1,64}$'.
    """
    # 'community-mysql___mysql_query' → 'community_mysql__mysql_query' (길이 < 64 검증)
    out = full_name.replace("___", "__").replace("-", "_")
    return out[:64]


# Gateway 가 자동으로 끼워넣는 내장 검색 도구 — 우리 도메인 도구가 아니므로 기본 제외.
_BUILTIN_TOOLS_TO_SKIP = {"x_amz_bedrock_agentcore_search"}


def build_mcp_tools(*,
                    client: MCPClient | None = None,
                    target_filter: list[str] | None = None,
                    max_response_chars: int = 12000) -> list[StructuredTool]:
    """Gateway 의 tools/list 를 받아 LangChain StructuredTool 리스트로 변환.

    Args:
        client: MCPClient 인스턴스 (없으면 default 생성).
        target_filter: 특정 Gateway target 만 노출 (None=전체). 예: ["community-mysql","awslabs-cloudwatch"].
                       각 도구 name 은 '<target>___<tool>' 형태.
        max_response_chars: 응답 truncate 한도.
    """
    cli = client or MCPClient()
    catalog = cli.list_tools()
    if not catalog:
        logger.warning("tools/list returned no tools — agent will run with no tools")
        return []

    out: list[StructuredTool] = []
    for spec in catalog:
        full_name = spec.get("name")
        if not full_name:
            continue
        if full_name in _BUILTIN_TOOLS_TO_SKIP:
            continue
        if target_filter:
            target = full_name.split("___", 1)[0] if "___" in full_name else full_name
            if target not in target_filter:
                continue

        description = (spec.get("description") or "").strip()
        if not description:
            description = f"Call MCP tool {full_name}"

        input_schema = spec.get("inputSchema") or {}
        safe_name = _safe_tool_name(full_name)
        args_model = _make_args_model(safe_name, input_schema)
        invoker = _make_invoker(cli, full_name, max_response_chars)

        tool = StructuredTool.from_function(
            func=invoker,
            name=safe_name,
            description=description,
            args_schema=args_model,
        )
        out.append(tool)

    logger.info("built %d MCP tools (filter=%s)", len(out), target_filter)
    return out
