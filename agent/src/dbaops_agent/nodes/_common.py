"""노드 공용 헬퍼 — offline 가드, LLM JSON 호출, 시간 유틸."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


def offline() -> bool:
    return os.environ.get("DBAOPS_OFFLINE", "").lower() in ("1", "true", "yes")


def utc_iso(seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def strip_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s


def llm_json(system: str, user: str, *, default: Any = None) -> Any:
    """LLM 호출 + JSON 파싱. offline / 에러 시 default 반환."""
    if offline():
        logger.debug("offline mode — returning default for llm_json")
        return default
    from ..llm import get_llm

    try:
        resp = get_llm().invoke([SystemMessage(content=system), HumanMessage(content=user)])
        text = strip_fence(getattr(resp, "content", str(resp)))
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_json failed (%s) — using default", e)
        return default


def time_range(state: dict, default_lookback_sec: int = 3600) -> tuple[str, str]:
    req = state.get("request") or {}
    tr = req.get("time_range") or {}
    start = tr.get("start") or utc_iso(default_lookback_sec)
    end = tr.get("end") or utc_iso(0)
    return start, end


def trace(node: str, summary: str, *, phase: str = "info", detail: dict[str, Any] | None = None,
          duration_ms: int | None = None, reasoning: str | None = None) -> dict[str, Any]:
    """Trace 이벤트 한 건을 만든다. 노드들이 반환 dict 의 'trace' 키에 list 로 담아 보낸다."""
    ev: dict[str, Any] = {"ts": utc_iso(0), "node": node, "phase": phase, "summary": summary}
    if reasoning:
        ev["reasoning"] = reasoning
    if detail is not None:
        ev["detail"] = detail
    if duration_ms is not None:
        ev["duration_ms"] = duration_ms
    return ev
