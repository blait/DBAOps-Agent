"""MCP client → AgentCore Gateway.

retry + tool_budget + dedup cache 직접 관리. ToolNode 미사용.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _cache_key(tool: str, params: dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, default=str)
    return f"{tool}:{hashlib.sha1(payload.encode()).hexdigest()}"


class MCPClient:
    """Phase 1 placeholder. Phase 2에서 실제 Gateway HTTP/MCP 호출 구현."""

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint or os.environ.get("GATEWAY_ENDPOINT", "")

    def call(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        cache: dict[str, Any] | None = None,
        budget: list[int] | None = None,
    ) -> Any:
        if cache is not None:
            key = _cache_key(tool, params)
            if key in cache:
                return cache[key]
        if budget is not None:
            if budget[0] <= 0:
                raise RuntimeError(f"tool_budget exhausted on {tool}")
            budget[0] -= 1
        # TODO: Gateway 호출 구현 (Phase 2)
        result: dict[str, Any] = {"tool": tool, "params": params, "data": None}
        if cache is not None:
            cache[_cache_key(tool, params)] = result
        return result
