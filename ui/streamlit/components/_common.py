"""뷰 공통 헬퍼."""

from __future__ import annotations

from typing import Any

SEV_BADGE = {"error": "🟥", "warn": "🟧", "info": "🟦"}
SEV_RANK = {"error": 0, "warn": 1, "info": 2}
DOMAIN_ICON = {"os": "🖥️", "db": "🗄️", "log": "📜"}


def severity_counts(findings: list[dict]) -> dict[str, int]:
    out = {"error": 0, "warn": 0, "info": 0}
    for f in findings or []:
        out[f.get("severity", "info")] = out.get(f.get("severity", "info"), 0) + 1
    return out


def by_domain(findings: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"os": [], "db": [], "log": []}
    for f in findings or []:
        d = f.get("domain", "?")
        out.setdefault(d, []).append(f)
    for v in out.values():
        v.sort(key=lambda f: SEV_RANK.get(f.get("severity", "info"), 9))
    return out


def find_by_id(findings: list[dict], fid: str) -> dict | None:
    for f in findings or []:
        if f.get("id") == fid:
            return f
    return None


def hypotheses_for(findings: list[dict], hypotheses: list[dict], fid: str) -> list[dict]:
    return [
        h for h in (hypotheses or [])
        if fid in (h.get("supporting_finding_ids") or [])
    ]


def render_evidence_block(st: Any, ev: Any) -> None:
    """evidence 배열을 보기 좋게 — dict면 표, str이면 bullet, list면 재귀."""
    if not ev:
        st.caption("(evidence 없음)")
        return
    if isinstance(ev, list):
        # 시계열 dict 들이면 표로
        if ev and all(isinstance(x, dict) and "ts" in x and "value" in x for x in ev):
            st.dataframe(ev, use_container_width=True, hide_index=True)
            return
        # 그 외는 한 줄씩
        for x in ev:
            if isinstance(x, dict):
                # 짧은 single-key dict 면 inline
                if len(x) == 1:
                    k, v = next(iter(x.items()))
                    st.markdown(f"- **{k}** — {v}")
                else:
                    cols = list(x.keys())
                    st.markdown("- " + " · ".join(f"**{k}**=`{x.get(k)}`" for k in cols))
            elif isinstance(x, str):
                st.markdown(f"- {x}")
            else:
                st.markdown(f"- {x}")
        return
    if isinstance(ev, dict):
        st.json(ev, expanded=False)
        return
    st.markdown(str(ev))


def conf_bar(conf: float) -> str:
    """0.0~1.0 confidence 를 8칸 ASCII bar 로."""
    n = max(0, min(8, int(round((conf or 0.0) * 8))))
    return "█" * n + "░" * (8 - n)
