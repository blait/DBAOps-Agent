"""Drain3 기반 로그 템플릿 추출 — Phase 2 활성화."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TemplateCount:
    template: str
    count: int


def classify(lines: list[str]) -> list[TemplateCount]:
    """Phase 2에서 drain3 TemplateMiner 로 교체."""
    counts: dict[str, int] = {}
    for line in lines:
        key = line.strip()[:120]
        counts[key] = counts.get(key, 0) + 1
    return [TemplateCount(template=t, count=c) for t, c in counts.items()]
