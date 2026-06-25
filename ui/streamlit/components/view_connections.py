"""🔌 MCP 연결설정 탭 — 올인원 EC2 에서 각 MCP 도구의 연결 정보를 편집.

connections.json(라우터와 공유, 기본 /data/connections.json)을 read/write 하고,
mcp-router 의 /healthz?tool=<target> 로 연결 테스트를 수행한다.

- 도구별 enabled 토글 + 연결 필드(host/port/db/user/password/url/region…)
- 저장 → connections.json write → 라우터가 mtime 감지해 자동 reload
- "연결 테스트" → 라우터 healthz 호출 → 해당 세션 tools/list 성공 여부
- infra_context(aurora writer id 등) 편집
"""

from __future__ import annotations

import json
import os
import urllib.request

import streamlit as st

CONNECTIONS_PATH = os.environ.get(
    "DBAOPS_CONNECTIONS_PATH",
    os.path.join(os.environ.get("DBAOPS_DATA_DIR", "/data"), "connections.json"),
)
ROUTER_HEALTH_URL = os.environ.get("MCP_ROUTER_HEALTH_URL", "http://mcp-router:9000/healthz")

ALL_TARGETS = [
    "rds-pi", "msk-metrics", "s3-log-fetch", "aws-api",
    "community-postgres", "community-mysql", "community-prometheus",
    "awslabs-cloudwatch", "awslabs-aws-doc", "awslabs-aws-api",
]

# target → (한글 라벨, 설명, 연결 필드 정의). 필드: (key, label, type) type ∈ {text, password, number}
_TARGET_META: dict[str, dict] = {
    "community-prometheus": {
        "label": "Prometheus (self-hosted)",
        "desc":  "node_exporter 등 호스트 메트릭. PromQL 쿼리.",
        "fields": [("PROMETHEUS_URL", "Prometheus URL (예: http://10.0.1.5:9090)", "text")],
    },
    "community-postgres": {
        "label": "PostgreSQL / Aurora PG",
        "desc":  "read-only SQL / EXPLAIN / health. user-pass 또는 Secret ARN 중 하나.",
        "fields": [
            ("PG_HOST", "Host", "text"),
            ("PG_PORT", "Port", "number"),
            ("PG_DBNAME", "Database", "text"),
            ("PG_USER", "User (Secret 미사용 시)", "text"),
            ("PG_PASSWORD", "Password (Secret 미사용 시)", "password"),
            ("PG_SECRET_ARN", "Secrets Manager ARN (선택)", "text"),
            ("PG_SSLMODE", "sslmode (require/disable)", "text"),
        ],
    },
    "community-mysql": {
        "label": "MySQL / RDS MySQL",
        "desc":  "read-only SELECT / EXPLAIN. user-pass 또는 Secret ARN.",
        "fields": [
            ("MYSQL_HOST", "Host", "text"),
            ("MYSQL_PORT", "Port", "number"),
            ("MYSQL_DB", "Database", "text"),
            ("MYSQL_USER", "User (Secret 미사용 시)", "text"),
            ("MYSQL_PASSWORD", "Password (Secret 미사용 시)", "password"),
            ("MYSQL_SECRET_ARN", "Secrets Manager ARN (선택)", "text"),
        ],
    },
    "awslabs-cloudwatch": {
        "label": "CloudWatch (awslabs)",
        "desc":  "메트릭/알람/Logs Insights. EC2 instance role 권한 사용 — 추가 입력 없음.",
        "fields": [],
    },
    "awslabs-aws-doc": {
        "label": "AWS Documentation (awslabs)",
        "desc":  "AWS 공식 문서 검색/조회. 외부 docs.aws.amazon.com — 추가 입력 없음.",
        "fields": [],
    },
    "awslabs-aws-api": {
        "label": "AWS API CLI (awslabs, read-only)",
        "desc":  "임의 read-only AWS CLI 명령. instance role 권한 사용.",
        "fields": [],
    },
    "rds-pi": {
        "label": "RDS Performance Insights (커스텀)",
        "desc":  "top SQL by AAS. instance role 의 pi:* 권한 사용 — 추가 입력 없음.",
        "fields": [],
    },
    "msk-metrics": {
        "label": "MSK / Kafka 메트릭 (커스텀)",
        "desc":  "AWS/Kafka CloudWatch 메트릭. 기본 토픽/CG 지정 가능.",
        "fields": [
            ("KAFKA_CLUSTER_NAME", "MSK Cluster Name", "text"),
            ("KAFKA_DEFAULT_TOPIC", "기본 Topic", "text"),
            ("KAFKA_DEFAULT_CG", "기본 Consumer Group", "text"),
        ],
    },
    "s3-log-fetch": {
        "label": "S3 로그 조회 (커스텀)",
        "desc":  "S3 gzip 로그 byte-range + regex. instance role 의 s3 read 권한 사용.",
        "fields": [],
    },
    "aws-api": {
        "label": "AWS API 묶음 (커스텀, read-only)",
        "desc":  "RDS/EC2/MSK describe + PI dimension. instance role 권한 사용.",
        "fields": [],
    },
}

_INFRA_FIELDS = [
    ("aurora_cluster_id", "Aurora/PG 클러스터 ID"),
    ("aurora_writer_id",  "Aurora/PG writer 인스턴스 ID"),
    ("aurora_reader_id",  "Aurora/PG reader 인스턴스 ID"),
    ("mysql_db_id",       "MySQL 인스턴스 ID"),
    ("prom_instance_id",  "Prometheus 호스트 EC2 instance-id"),
    ("msk_cluster_name",  "MSK 클러스터 이름"),
    ("log_bucket",        "로그 S3 버킷명"),
]


def _load() -> dict:
    try:
        with open(CONNECTIONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONNECTIONS_PATH) or ".", exist_ok=True)
    tmp = CONNECTIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONNECTIONS_PATH)


def _health(target: str | None = None) -> dict:
    url = ROUTER_HEALTH_URL + (f"?tool={target}" if target else "")
    try:
        with urllib.request.urlopen(url, timeout=50) as resp:
            return json.loads(resp.read()).get("targets", {})
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def render() -> None:
    st.markdown("### 🔌 MCP 연결 설정")
    st.caption(
        "각 분석 도구가 붙을 대상(DB / Prometheus / AWS)을 설정합니다. "
        "저장하면 라우터가 자동으로 반영합니다. "
        f"설정 파일: `{CONNECTIONS_PATH}`"
    )

    cfg = _load()
    cfg.setdefault("aws_region", os.environ.get("AWS_REGION", "ap-northeast-2"))
    cfg.setdefault("bedrock_model_id",
                   os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7"))
    cfg.setdefault("tools", {})
    cfg.setdefault("infra_context", {})

    # ─── 전역 ───
    with st.container(border=True):
        c1, c2 = st.columns(2)
        cfg["aws_region"] = c1.text_input("AWS Region", cfg["aws_region"])
        cfg["bedrock_model_id"] = c2.text_input("Bedrock Model ID", cfg["bedrock_model_id"])

    # ─── 라우터 상태 한눈에 ───
    if st.button("🔄 전체 연결 상태 새로고침"):
        st.session_state["_mcp_health"] = _health()
    health = st.session_state.get("_mcp_health", {})
    if health.get("_error"):
        st.warning(f"라우터 상태 조회 실패: {health['_error']} (라우터가 떠있는지 확인)")

    # ─── 도구별 카드 ───
    new_tools: dict[str, dict] = {}
    for target in ALL_TARGETS:
        meta = _TARGET_META[target]
        cur = cfg["tools"].get(target, {})
        hstat = health.get(target, {}) if isinstance(health, dict) else {}

        badge = ""
        if hstat.get("ok") is True:
            badge = f"  ✅ {hstat.get('tools', 0)} tools"
        elif hstat.get("ok") is False:
            badge = "  ❌ 연결 실패"

        with st.expander(f"{meta['label']}  (`{target}`){badge}",
                         expanded=bool(cur.get("enabled"))):
            st.caption(meta["desc"])
            enabled = st.toggle("사용", value=bool(cur.get("enabled")),
                                key=f"en__{target}")
            conf: dict = {"enabled": enabled}
            for fkey, flabel, ftype in meta["fields"]:
                val = cur.get(fkey, "")
                if ftype == "password":
                    conf[fkey] = st.text_input(flabel, value=val, type="password",
                                               key=f"f__{target}__{fkey}")
                elif ftype == "number":
                    conf[fkey] = st.text_input(flabel, value=str(val) if val else "",
                                               key=f"f__{target}__{fkey}")
                else:
                    conf[fkey] = st.text_input(flabel, value=val,
                                               key=f"f__{target}__{fkey}")
            # 빈 문자열 필드는 굳이 저장하지 않음 (Secret/직접입력 혼동 방지)
            conf = {k: v for k, v in conf.items() if k == "enabled" or v}
            new_tools[target] = conf

            if st.button("🔌 연결 테스트", key=f"test__{target}", disabled=not enabled):
                # 테스트 전에 현재 편집값을 먼저 저장해야 라우터가 그 설정으로 연결
                cfg["tools"] = {**cfg["tools"], **new_tools}
                _save(cfg)
                res = _health(target)
                st.session_state["_mcp_health"] = {**health, **(res if isinstance(res, dict) else {})}
                one = res.get(target, {}) if isinstance(res, dict) else {}
                if one.get("ok"):
                    st.success(f"연결 성공 — {one.get('tools', 0)} tools")
                else:
                    st.error(f"연결 실패: {one.get('error', res.get('_error', 'unknown'))}")

    # ─── infra_context ───
    with st.container(border=True):
        st.markdown("#### 인프라 식별자 (분석 프롬프트가 참조)")
        st.caption("비워두면 에이전트가 describe 도구로 직접 찾습니다.")
        ic = cfg["infra_context"]
        cols = st.columns(2)
        new_ic: dict = {}
        for i, (key, label) in enumerate(_INFRA_FIELDS):
            new_ic[key] = cols[i % 2].text_input(label, value=ic.get(key, ""),
                                                  key=f"ic__{key}")
        new_ic = {k: v for k, v in new_ic.items() if v}

    # ─── 저장 ───
    if st.button("💾 전체 저장", type="primary", use_container_width=True):
        cfg["tools"] = new_tools
        cfg["infra_context"] = new_ic
        _save(cfg)
        st.success("저장 완료 — 라우터가 다음 호출부터 반영합니다.")
        st.session_state["_mcp_health"] = _health()
        st.rerun()
