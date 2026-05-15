"""Deterministic anomaly detection — z-score + EWMA + simple change-point."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnomalyPoint:
    ts: str
    value: float
    z: float
    reason: str


def _mean_std(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 1e-9
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, max(var**0.5, 1e-9)


def zscore(values: list[float]) -> list[float]:
    mean, std = _mean_std(values)
    return [(v - mean) / std for v in values]


def ewma(values: list[float], alpha: float = 0.3) -> list[float]:
    """Exponentially weighted moving average."""
    out: list[float] = []
    s = 0.0
    for i, v in enumerate(values):
        s = v if i == 0 else alpha * v + (1 - alpha) * s
        out.append(s)
    return out


def changepoints(values: list[float], window: int = 10, ratio: float = 2.0) -> set[int]:
    """단순 means-shift change-point: 이전 윈도와 다음 윈도 평균비가 `ratio` 이상이면 표시."""
    out: set[int] = set()
    n = len(values)
    if n < 2 * window:
        return out
    for i in range(window, n - window):
        prev = values[i - window : i]
        nxt = values[i : i + window]
        mp, mn = sum(prev) / window, sum(nxt) / window
        if mp == 0:
            continue
        r = mn / mp if mp != 0 else 0
        if r >= ratio or (mn != 0 and mp / mn >= ratio):
            out.add(i)
    return out


def detect(
    series: list[tuple[str, float]],
    *,
    z_threshold: float = 3.0,
    ewma_alpha: float = 0.3,
    cp_window: int = 10,
    cp_ratio: float = 2.0,
) -> list[AnomalyPoint]:
    """z-score + EWMA 잔차 + change-point 결합."""
    if not series:
        return []
    values = [v for _, v in series]
    zs = zscore(values)
    smooth = ewma(values, alpha=ewma_alpha)
    residual = [v - s for v, s in zip(values, smooth)]
    rz = zscore(residual)
    cps = changepoints(values, window=cp_window, ratio=cp_ratio)

    out: list[AnomalyPoint] = []
    for i, (ts, v) in enumerate(series):
        reasons: list[str] = []
        if abs(zs[i]) >= z_threshold:
            reasons.append("zscore")
        if abs(rz[i]) >= z_threshold:
            reasons.append("ewma_residual")
        if i in cps:
            reasons.append("changepoint")
        if reasons:
            out.append(AnomalyPoint(ts=ts, value=v, z=zs[i], reason="+".join(reasons)))
    return out
