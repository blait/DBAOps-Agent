"""도구 카탈로그 통합 — 커스텀 4 + stdio 6. connections.json 변경 시 stdio 세션 재구성.

AgentCore Gateway 의 역할을 router 안에서 재현:
  - tools/list  : enabled 된 모든 target 의 도구를 '<target>___<tool>' 이름으로 합쳐 반환
  - tools/call  : 이름에서 target 을 분리해 커스텀 핸들러 또는 stdio 세션으로 위임

connections.json 의 mtime 을 매 요청 직전에 확인해, 바뀌었으면 stdio 세션을 lazy 재구성한다.
(UI 가 연결설정을 저장하면 다음 호출부터 자동 반영.)
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import connections
from .custom_tools import CustomToolHost
from .stdio_proxy import StdioProxy

logger = logging.getLogger(__name__)


class Registry:
    def __init__(self) -> None:
        self._custom = CustomToolHost()
        self._stdio = StdioProxy()
        self._lock = threading.Lock()
        self._cfg: dict[str, Any] = {}
        self._cfg_mtime: float = -1.0
        self.reload()

    # ─────────── config 동기화 ───────────

    def reload(self, force: bool = False) -> None:
        """connections.json 이 바뀌었으면 다시 읽고 stdio 세션 재구성."""
        m = connections.mtime()
        if not force and m == self._cfg_mtime:
            return
        with self._lock:
            # double-check under lock
            m = connections.mtime()
            if not force and m == self._cfg_mtime:
                return
            cfg = connections.load()
            self._cfg = cfg
            self._cfg_mtime = m
            region = cfg.get("aws_region", "ap-northeast-2")

            enabled = set(connections.enabled_targets(cfg))

            # stdio: enabled 인 것만 연결, 아닌 것은 끊기.
            for target in connections.STDIO_TARGETS:
                conf = cfg["tools"].get(target, {})
                if target in enabled:
                    spec = connections.stdio_spec(target, conf, region)
                    if spec is None:
                        logger.warning("stdio %s enabled but spec incomplete — skip", target)
                        self._stdio.disconnect(target)
                        continue
                    try:
                        self._stdio.ensure(target, spec)
                    except Exception as e:  # noqa: BLE001
                        logger.error("stdio %s connect error: %s", target, e)
                else:
                    self._stdio.disconnect(target)
            logger.info("registry reloaded — enabled targets: %s", sorted(enabled))

    # ─────────── MCP 메서드 ───────────

    def list_tools(self) -> list[dict[str, Any]]:
        self.reload()
        cfg = self._cfg
        enabled = set(connections.enabled_targets(cfg))
        tools: list[dict[str, Any]] = []

        for target in connections.CUSTOM_TARGETS:
            if target in enabled:
                try:
                    tools.extend(self._custom.list_tools(target))
                except Exception as e:  # noqa: BLE001
                    logger.error("custom list_tools %s failed: %s", target, e)

        for target in connections.STDIO_TARGETS:
            if target in enabled:
                tools.extend(self._stdio.list_tools(target))

        return tools

    def call_tool(self, full_name: str, args: dict[str, Any]) -> Any:
        self.reload()
        if "___" not in full_name:
            raise ValueError(f"tool name missing namespace: {full_name}")
        target, sub_tool = full_name.split("___", 1)

        if target in connections.CUSTOM_TARGETS:
            return self._custom.call(target, sub_tool, args)
        if target in connections.STDIO_TARGETS:
            return self._stdio.call(target, sub_tool, args)
        raise ValueError(f"unknown target: {target}")

    # ─────────── health ───────────

    def health(self, target: str | None = None) -> dict[str, Any]:
        """전체 또는 특정 target 상태. UI 연결테스트용."""
        self.reload()
        cfg = self._cfg
        enabled = set(connections.enabled_targets(cfg))
        connected = set(self._stdio.connected_targets())

        def _status(t: str) -> dict[str, Any]:
            if t not in enabled:
                return {"enabled": False, "ok": None, "tools": 0}
            if t in connections.CUSTOM_TARGETS:
                try:
                    n = len(self._custom.list_tools(t))
                    return {"enabled": True, "ok": True, "tools": n}
                except Exception as e:  # noqa: BLE001
                    return {"enabled": True, "ok": False, "error": str(e), "tools": 0}
            # stdio
            if t in connected:
                return {"enabled": True, "ok": True, "tools": len(self._stdio.list_tools(t))}
            # enabled 인데 연결 안 됨 → 재시도
            conf = cfg["tools"].get(t, {})
            spec = connections.stdio_spec(t, conf, cfg.get("aws_region", "ap-northeast-2"))
            if spec is None:
                return {"enabled": True, "ok": False, "error": "connection config incomplete", "tools": 0}
            try:
                toolset = self._stdio.ensure(t, spec)
                return {"enabled": True, "ok": True, "tools": len(toolset)}
            except Exception as e:  # noqa: BLE001
                return {"enabled": True, "ok": False, "error": str(e), "tools": 0}

        if target:
            return {target: _status(target)}
        return {t: _status(t) for t in connections.ALL_TARGETS}


_registry: Registry | None = None
_registry_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = Registry()
    return _registry
