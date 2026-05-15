"""Deterministic anomaly detection — z-score + EWMA + change-point."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnomalyPoint:
    ts: str
    value: float
    z: float
    reason: str


def zscore(values: list[float]) -> list[float]:
    if not values:
        return []
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = var**0.5 or 1e-9
    return [(v - mean) / std for v in values]


def detect(series: list[tuple[str, float]], z_threshold: float = 3.0) -> list[AnomalyPoint]:
    """Phase 4에서 EWMA + change-point 추가. 여기는 z-score 기준선."""
    if not series:
        return []
    zs = zscore([v for _, v in series])
    out: list[AnomalyPoint] = []
    for (ts, v), z in zip(series, zs):
        if abs(z) >= z_threshold:
            out.append(AnomalyPoint(ts=ts, value=v, z=z, reason="z_score"))
    return out
