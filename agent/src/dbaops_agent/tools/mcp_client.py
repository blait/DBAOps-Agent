"""MCP client → AgentCore Gateway.

retry + tool_budget + dedup cache 직접 관리. ToolNode 미사용.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def _cache_key(tool: str, params: dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, default=str)
    return f"{tool}:{hashlib.sha1(payload.encode()).hexdigest()}"


class MCPClient:
    """AgentCore Gateway MCP 호출 클라이언트.

    Phase 1: GATEWAY_ENDPOINT 가 비어있으면 stub 반환.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        self.endpoint = (endpoint or os.environ.get("GATEWAY_ENDPOINT", "")).rstrip("/")
        self.token = token or os.environ.get("GATEWAY_BEARER_TOKEN", "")
        self.timeout = timeout
        self.max_retries = max_retries

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
                logger.debug("cache hit %s", key)
                return cache[key]
        if budget is not None:
            if budget[0] <= 0:
                raise RuntimeError(f"tool_budget exhausted on {tool}")
            budget[0] -= 1

        result = self._invoke(tool, params)

        if cache is not None:
            cache[_cache_key(tool, params)] = result
        return result

    def _invoke(self, tool: str, params: dict[str, Any]) -> Any:
        if not self.endpoint:
            logger.warning("GATEWAY_ENDPOINT empty — returning stub for %s", tool)
            return {"tool": tool, "params": params, "stub": True}

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": _cache_key(tool, params),
                "method": "tools/call",
                "params": {"name": tool, "arguments": params},
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                if "error" in data:
                    raise RuntimeError(f"MCP error: {data['error']}")
                return data.get("result", data)
            except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
                last_err = e
                wait = 0.5 * (2**attempt)
                logger.warning("MCP call %s attempt %d failed: %s (retry in %.1fs)", tool, attempt + 1, e, wait)
                time.sleep(wait)
        raise RuntimeError(f"MCP call {tool} failed after {self.max_retries + 1} attempts: {last_err}")
