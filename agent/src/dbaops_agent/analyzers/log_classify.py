"""Drain3-backed 로그 템플릿 추출. drain3 미설치 / 실패 시 prefix-bucket fallback."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TemplateCount:
    template: str
    count: int


_NUM = re.compile(r"\b\d+\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _normalize(line: str) -> str:
    s = _UUID.sub("<UUID>", line)
    s = _IP.sub("<IP>", s)
    s = _HEX.sub("<HEX>", s)
    s = _NUM.sub("<N>", s)
    return s.strip()


def _fallback(lines: list[str]) -> list[TemplateCount]:
    counts: dict[str, int] = {}
    for line in lines:
        key = _normalize(line)[:200]
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return [TemplateCount(template=t, count=c) for t, c in counts.items()]


def classify(lines: list[str]) -> list[TemplateCount]:
    if not lines:
        return []
    try:
        from drain3 import TemplateMiner  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.debug("drain3 unavailable (%s) — using fallback", e)
        return _fallback(lines)

    miner = TemplateMiner()
    by_id: dict[int, dict] = {}
    for line in lines:
        if not line.strip():
            continue
        r = miner.add_log_message(line.strip())
        if not r:
            continue
        cid = r["cluster_id"]
        bucket = by_id.setdefault(cid, {"template": r["template_mined"], "count": 0})
        bucket["count"] += 1
    return [TemplateCount(template=v["template"], count=v["count"]) for v in by_id.values()]


def top_n(items: list[TemplateCount], n: int = 20) -> list[TemplateCount]:
    return sorted(items, key=lambda x: x.count, reverse=True)[:n]
