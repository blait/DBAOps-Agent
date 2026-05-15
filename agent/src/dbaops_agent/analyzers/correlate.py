"""시간 윈도 join — 도메인 간 사건/시계열을 동일 윈도로 묶는다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def _parse(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.now(timezone.utc)


@dataclass
class CorrelatedEvent:
    bucket: str
    sources: dict[str, list[dict]]


def bucketize(
    events_by_source: dict[str, list[dict]],
    *,
    window_sec: int = 60,
    ts_field: str = "ts",
) -> list[CorrelatedEvent]:
    """events_by_source = {source_name: [{"ts": iso, ...}, ...]}.

    window_sec 단위 시간 버킷에 source 별로 묶는다. 두 source 이상이 함께 든 버킷이 가치 있음.
    """
    buckets: dict[str, dict[str, list[dict]]] = {}
    for source, events in events_by_source.items():
        for ev in events:
            ts = ev.get(ts_field)
            if not ts:
                continue
            t = _parse(ts)
            base = t - timedelta(seconds=t.second % window_sec, microseconds=t.microsecond)
            key = base.isoformat(timespec="seconds")
            buckets.setdefault(key, {}).setdefault(source, []).append(ev)

    out: list[CorrelatedEvent] = []
    for key in sorted(buckets):
        out.append(CorrelatedEvent(bucket=key, sources=buckets[key]))
    return out


def cross_source(corr: list[CorrelatedEvent], min_sources: int = 2) -> list[CorrelatedEvent]:
    """두 개 이상 source 가 동시에 발화한 버킷만 추출."""
    return [c for c in corr if len(c.sources) >= min_sources]
