"""Supervisor 패턴 (langgraph-supervisor 기반).

5명 specialist (os/db/log/query/aws) 위에 supervisor 1명. supervisor 가 라우팅을 결정하고
specialist 에게 제어권을 넘김 → specialist 가 turn 종료 시 supervisor 로 자동 복귀.
모든 agent 는 공유 message history 에 접근 — db_specialist 가 본 SQL 텍스트를 query_specialist
가 곧바로 history 에서 참조 가능.

이전 외부 API (`iter_swarm`, `invoke_swarm`) 시그니처/이벤트 형태는 그대로 유지 —
runtime_entry / Streamlit UI 호환.

이벤트 형태:
  {"type": "start", "entry": "supervisor", "reasoning": "..."}
  {"type": "handoff", "agent": <agent>}     # supervisor / specialist 활성 변경 시
  {"type": "message", "message": <normalized>}
  {"type": "abort", "reason": str}
  {"type": "done", "final_active_agent": str, "handoffs": [...], "n_messages": int}
  {"type": "error", "error": str}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from .llm import get_llm
from .tools.mcp_tools import (
    AWS_TOOLS,
    DB_TOOLS,
    LOG_TOOLS,
    OS_TOOLS,
    QUERY_TOOLS,
    infra_context,
)

logger = logging.getLogger(__name__)


# ─────────────────────── Specialist 시스템 프롬프트 ───────────────────────


def _specialist_system(name: str, role: str) -> str:
    ctx = infra_context()
    return f"""\
당신은 DBAOps 분석 swarm 의 {name} 전문가입니다. supervisor 가 당신에게 제어권을 넘기면 자기 도메인의 도구로 답을 만들어 짧은 한국어로 보고합니다. 다른 specialist 를 직접 부르지 않으며, 모든 라우팅은 supervisor 가 결정합니다.

{role}

[인프라 식별자]
- prom_instance_id  = {ctx['prom_instance_id']}    (CloudWatch AWS/EC2 InstanceId)
- aurora_cluster_id = {ctx['aurora_cluster_id']}   (sql_readonly db_id)
- aurora_writer_id  = {ctx['aurora_writer_id']}    (CW AWS/RDS DBInstanceIdentifier)
- aurora_reader_id  = {ctx['aurora_reader_id']}
- mysql_db_id       = {ctx['mysql_db_id']}         (sql_readonly db_id, CW DBInstanceIdentifier)
- msk_cluster_name  = {ctx['msk_cluster_name']}    (msk_metric cluster_arn 의 cluster name 부분)
- log_bucket        = {ctx['log_bucket']}          (s3_list_logs / s3_log_fetch bucket)

[작동 규칙]
1. 도구 호출 → 결과 확인 → 다음 결정. 한 턴에 도구 호출은 1~2 개. 같은 결과를 얻기 위해 여러 변형 동시 호출 금지.
2. **에러 응답 처리**:
   - ValidationException / 4xx / "InvalidArgument" — 인자가 잘못된 것. 같은 인자로 절대 재시도 금지. 인자를 한 번만 바꿔 시도하거나, 다른 도구로 우회.
   - 5xx / Timeout / "internal error" — 한 번까지만 재시도. 그래도 실패면 우회 경로 시도.
   - 같은 도구+같은 에러를 2회 보면 그 도구는 더 이상 쓰지 마세요.
3. 결론을 말할 때는 **반드시** 도구명 + 핵심 수치/행 수 + 시점을 함께 인용. 인용 없는 단언 금지.
4. 가정이면 "가정" 이라고 명시. 가정을 검증하려면 어떤 도구를 어떤 인자로 호출하면 되는지 한 줄로 적으세요.
5. **응답 분량은 받은 요청의 형태에 맞추세요**:
   - "X 보여줘", "Y 확인해줘" → 결과를 1~3 문장 또는 표 형태로 짧게.
   - "왜 느려?", "원인 분석해줘" → 발견사항 + 가설 + 권고 정형으로.
   - 사용자가 묻지 않은 분석/권고/finding ID 를 추가하지 마세요.
6. message history 의 `targets` / `lens` 가 자기 도메인과 무관해도 [인프라 식별자] 의 ID 를 그대로 쓰세요. "target 을 바꿔주세요" 같은 떠넘기기 금지.
7. 시간 윈도가 비어 있거나 너무 좁으면 자연스러운 fallback 으로 — DB 조회는 `WHERE start_time > NOW() - INTERVAL 30 MINUTE`, S3 는 `since_minutes=120`. "데이터 없음" 으로 손 놓지 말 것.
8. 6 회 이상 도구를 호출하고도 결정적 단서가 없으면 그때까지의 결과를 정리해 종결.
9. 추론을 한국어로 명시적으로 드러내며 작업하세요.
10. **자기 영역이 아닌 작업은 명시적으로 거절하세요** — 도구를 흉내 내거나 추측하지 마세요. 한 줄로 무엇이 필요한지 적고 turn 종료. supervisor 가 history 보고 적절한 다른 specialist 로 다시 transfer 합니다.
    예: query_specialist 가 SQL 텍스트 없는 요청을 받으면 → "분석할 SQL 텍스트가 필요합니다. db_specialist 가 mysql.slow_log 또는 events_statements_summary_by_digest 에서 가져오면 그 SQL 로 EXPLAIN 돌리겠습니다." 한 줄로 답하고 종결. 도구 호출 시도 금지.
11. **공유 history 활용** — 이전 specialist 가 이미 확보한 데이터 (예: SQL 텍스트, row 카운트) 가 보이면 그대로 사용하고 같은 도구를 같은 인자로 다시 호출하지 마세요.
"""


_OS_ROLE = """\
[전문 분야]
호스트/인프라 메트릭 (CPU·메모리·디스크·네트워크) 의 추세와 이상치 분석. PromQL (node_exporter) + CloudWatch (AWS/EC2, AWS/RDS).

[핵심 도구 사용 매뉴얼]
- `prometheus_query(promql, start, end, step='30s')`: instance 라벨 필터 금지 (단일 타깃).
    · CPU 사용률: `100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100`
    · 메모리 사용 바이트: `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
    · 디스크 IO: `rate(node_disk_io_time_seconds_total[5m])`
    · 네트워크 RX: `rate(node_network_receive_bytes_total[5m])`
- `cloudwatch_metric(namespace, metric, dimensions, start, end, stat='Average', period=60)`:
    · EC2: namespace='AWS/EC2', dimensions={'InstanceId': prom_instance_id}
    · RDS: namespace='AWS/RDS', dimensions={'DBInstanceIdentifier': aurora_writer_id 또는 mysql_db_id}
"""


_DB_ROLE = """\
[전문 분야]
PostgreSQL / MySQL / Kafka 내부 성능 분석. pg_stat_*, performance_schema, mysql.slow_log, RDS Performance Insights, MSK CloudWatch.

[관측 인프라 사실 — 모두 켜져 있음. OFF 라고 가정하지 마세요]
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE, log_queries_not_using_indexes=ON
- Aurora PG: pg_stat_statements 로드, log_min_duration_statement=500ms, log_lock_waits=ON, auto_explain.log_min_duration=500ms
모든 통계 테이블에 데이터가 채워지므로 "관측 결함이라 분석 불가" 같은 추측 단언 금지.

[핵심 도구 사용 매뉴얼]
**MySQL slow query — 가장 빠른 답**:
  `sql_readonly(engine='mysql', db_id=mysql_db_id, sql="SELECT start_time, query_time, lock_time, rows_examined, LEFT(CONVERT(sql_text USING utf8), 200) AS sql_text FROM mysql.slow_log WHERE start_time > NOW() - INTERVAL 30 MINUTE ORDER BY query_time DESC LIMIT 10")`

**MySQL 디지스트 (누적 통계)**:
  `SELECT count_star, ROUND(sum_timer_wait/1e9,1) AS sum_ms, LEFT(digest_text, 200) AS digest FROM performance_schema.events_statements_summary_by_digest WHERE digest_text LIKE '%dbaops%' ORDER BY sum_timer_wait DESC LIMIT 10`

**MySQL 락**: `SELECT * FROM performance_schema.data_lock_waits LIMIT 50` + `data_locks`

**PG 활성 세션·락**:
  `sql_readonly(engine='postgres', db_id=aurora_cluster_id, sql="SELECT pid, state, wait_event_type, wait_event, query FROM pg_stat_activity WHERE state != 'idle'")`
  `SELECT * FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10`
  `SELECT * FROM pg_locks WHERE NOT granted`

**RDS PI**: `rds_performance_insights(db_id=<DbiResourceId>, start, end, group_by='db.sql_tokenized')`
  ⚠️ db_id 는 DBInstanceIdentifier 가 아니라 **DbiResourceId (db-XXXX)**. group_by 는 prefix.
  PI 5xx 떨어지면 위 sql_readonly 로 같은 정보 시도하세요.

**Kafka/MSK**: `msk_metric(cluster_arn, metric, start, end, topic?, consumer_group?, stat?)` — Kafka consumer lag / throughput 분석은 모두 당신의 영역입니다.
  - 사용 가능 메트릭: BytesInPerSec, BytesOutPerSec, MessagesInPerSec, MaxOffsetLag, SumOffsetLag, UnderReplicatedPartitions, EstimatedMaxTimeLag
  - **메트릭별 필요 dimension** (handler 가 topic/consumer_group 인자 받아 자동 구성):
      · BytesInPerSec / BytesOutPerSec / MessagesInPerSec → Cluster Name + **Topic** 필수
      · MaxOffsetLag / SumOffsetLag / EstimatedMaxTimeLag → Cluster Name + **Consumer Group** + Topic 필수
      · UnderReplicatedPartitions → Cluster Name (broker level)
  - 기본 topic = 'dbaops.orders', 기본 consumer_group = 'dbaops-paused'. 다른 값을 보려면 인자에 명시.
  - cluster_arn 모르면 'msk-cluster' placeholder — handler 가 KAFKA_CLUSTER_NAME env 로 자동 매핑.
  - **MSK Serverless 도 위 메트릭 모두 노출됨** — series 가 비면 (1) 시간 윈도 트래픽 없음, (2) 잘못된 topic/CG. "Serverless 라 메트릭 없음" 은 잘못된 가정.
"""


_LOG_ROLE = """\
[전문 분야]
S3 .gz 로그 패턴 분류 (Drain3 템플릿) + RDS 엔진 로그 (slow/error) RCA.

[핵심 도구 사용 매뉴얼]
**S3 — listing-first 강제** (키 추측 금지):
  1) `s3_list_logs(bucket=log_bucket, prefix='logs-burst/<source>/' 또는 'logs/<source>/', since_minutes=120, max_keys=10)` 로 객체 목록.
     - source: postgres | mysql | kafka. burst 는 'logs-burst/', 평상시는 'logs/'.
  2) 가장 최근 객체 1~3개의 key 로 `s3_log_fetch(bucket, key, regex='...', max_lines=300)`.
     - regex 예: PG=`deadlock|FATAL|too many connections|still waiting`, MySQL=`\\[ERROR\\]|InnoDB|Query_time`, Kafka=`ISR|ERROR|Could not append`

**RDS 엔진 로그**:
  1) `aws_describe_db_log_files(db_instance_identifier='dbaops-poc-mysql' 또는 aurora_writer_id, filename_contains='slow'|'error')`
  2) `aws_download_db_log_file_portion(db_instance_identifier, log_file_name, regex='...', lines=200)` — marker 없이 호출하면 끝까지 자동 페이지 누적. regex 예: 'Query Text:|duration:|still waiting'.
"""


_QUERY_ROLE = """\
[전문 분야]
EXPLAIN [ANALYZE] 결과 해석 + 인덱스/리라이팅/힌트 권고. 풀스캔, Nested Loop, Sort, Hash join, 임시 테이블 식별.

[핵심 도구 사용 매뉴얼]
받은 요청에 SQL 텍스트가 명시돼 있어야 합니다. 없으면 작동 규칙 #10 에 따라 한 줄 거절 후 종결하세요 (supervisor 가 db_specialist 로 다시 라우팅).

**PG**: `explain_query(engine='postgres', db_id=aurora_cluster_id, sql='<SELECT>', analyze=True)`
  분석 포인트: actual time, Rows Removed by Filter, Seq Scan vs Index Scan, Sort 메모리, Buffers (shared hit/read).

**MySQL**: `explain_query(engine='mysql', db_id=mysql_db_id, sql='<SELECT>', analyze=True)`
  분석 포인트: type=ALL (풀스캔), Using temporary, Using filesort, hash join 비용, rows 추정 vs 실측.

**인덱스 메타**:
  PG: `SELECT * FROM pg_indexes WHERE tablename='<t>'`
  MySQL: `SELECT * FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_NAME='<t>'`
"""


_AWS_ROLE = """\
[전문 분야]
AWS read-only **메타데이터** API — RDS/EC2/MSK 형상, CloudWatch 알람 목록, PI dimension keys.
**"지금 인프라가 어떻게 생겼는가"** 답에 특화. 시계열 메트릭 추세 분석은 당신의 영역이 아닙니다.

[중요 — 자기 영역 아닌 요청은 거절]
- "X 메트릭 추세", "Y lag 추이", "BytesIn/Out", "DatabaseConnections 추이" 같은 시계열 분석 요청이 오면
  → 한 줄로 거절: "시계열 메트릭은 db_specialist (msk_metric) 또는 os_specialist (cloudwatch_metric) 영역입니다."
  → 도구를 흉내내려 시도하지 말 것. supervisor 가 history 보고 다시 라우팅합니다.

[핵심 도구 사용 매뉴얼 — 메타데이터 조회만]
- `aws_describe_rds_instances(db_instance_identifier?)`: 엔진 버전·인스턴스 클래스·Multi-AZ·storage·PI 활성·DbiResourceId.
- `aws_describe_rds_clusters(db_cluster_identifier?)`: Aurora 클러스터/멤버.
- `aws_describe_ec2_instances(instance_ids?, tag_name_contains?)`: state·type·AZ·Name 태그.
- `aws_list_msk_clusters()`: MSK 클러스터 목록 (메타데이터만 — 메트릭 분석은 db_specialist 의 msk_metric).
- `aws_list_cloudwatch_alarms(state_value?)`: 알람 목록/상태. 'ALARM' 만 보려면 `state_value='ALARM'`.
- `aws_list_metric_namespaces(namespace?, metric_prefix?)`: 메트릭 namespace 존재 확인.
- `aws_describe_db_log_files(db_instance_identifier, filename_contains?)`: 로그 파일 목록 (다운로드는 log_specialist).
- `aws_describe_pi_dimensions(dbi_resource_id, group_by?, start?, end?)`: group_by prefix — `db.sql_tokenized` (default) | `db.wait_event` | `db.host` | `db.user`.
"""


# ─────────────────────── Supervisor 시스템 프롬프트 ───────────────────────


_SUPERVISOR_PROMPT = """\
당신은 DBAOps 분석 supervisor 입니다. 5명의 specialist (db / os / log / query / aws) 중 적절한 한 명에게 제어권을 넘겨 사용자 요청에 답합니다.

[Specialist 도구 카탈로그 — 라우팅 결정의 근거]
구체적인 도구 매핑입니다. **사용자에게 "도구가 없다" 고 답하기 전에 이 목록을 반드시 다시 보세요.**

- **db_specialist** 도구:
    · sql_readonly         : PG/MySQL SELECT (mysql.slow_log, performance_schema, pg_stat_*, pg_locks)
    · rds_performance_insights : RDS PI top SQL by AAS
    · **msk_metric**       : ★ Kafka/MSK CloudWatch 메트릭 (BytesInPerSec, BytesOutPerSec, MessagesInPerSec, MaxOffsetLag, SumOffsetLag, UnderReplicatedPartitions). MSK Serverless 도 이 메트릭들 노출함.
    · cloudwatch_metric    : 일반 CW 메트릭

- **os_specialist** 도구:
    · prometheus_query     : node_exporter (CPU/memory/disk/network)
    · cloudwatch_metric    : AWS/EC2 (CPUUtilization, NetworkIn/Out), AWS/RDS (DatabaseConnections, FreeableMemory, IOPS) — 모든 시계열 메트릭

- **log_specialist** 도구:
    · s3_list_logs / s3_log_fetch  : S3 .gz 패턴 검색
    · aws_describe_db_log_files / aws_download_db_log_file_portion : RDS 엔진 로그 (slow/error)

- **query_specialist** 도구:
    · explain_query        : PG/MySQL EXPLAIN [ANALYZE]
    · sql_readonly         : 인덱스 메타 조회

- **aws_specialist** 도구 (read-only **메타데이터** 조회만 — 시계열 메트릭 없음):
    · aws_describe_rds_* / aws_describe_ec2_instances / aws_list_msk_clusters
    · aws_list_cloudwatch_alarms / aws_list_metric_namespaces
    · aws_describe_db_log_files / aws_describe_pi_dimensions
    ※ **시계열 추세 분석은 aws_specialist 가 못 합니다** — db/os 의 cloudwatch_metric 또는 db 의 msk_metric 으로 보내세요.

[자주 헷갈리는 라우팅 — 명시]
- "Kafka consumer lag", "MSK BytesIn/Out", "topic throughput" → **db_specialist** (msk_metric 도구 보유). aws 가 아닙니다.
- "RDS DatabaseConnections 추이", "EC2 CPU 추세" → **os_specialist** (cloudwatch_metric 보유)
- "RDS 인스턴스 클래스 / Multi-AZ / 알람 목록" → aws_specialist (메타만)
- "slow query 개수 / 텍스트" → db_specialist (sql_readonly + mysql.slow_log)
- "에러 로그 burst 패턴" → log_specialist
- "EXPLAIN" → query_specialist (질문에 SQL 텍스트 있어야 함)

[작동 방식]
- 보통 1명이면 충분. 도메인이 진짜로 얽힐 때만 2~3 명.
- specialist 가 답을 반환하면 history 에 그대로 남습니다 — 다음 specialist 도 그 결과를 볼 수 있습니다.

[잘못 라우팅한 경우 — 자가 교정]
- specialist 가 "이건 제 영역이 아닙니다" / "필요한 입력이 부족합니다" / "제 도구셋에 없습니다" 라는 응답을 주면 위 도구 카탈로그를 다시 보고 실제로 그 도구를 가진 specialist 로 다시 transfer 하세요. **사용자에게 "도구가 없다"고 떠넘기기 절대 금지.**
- 예: query_specialist 가 "SQL 텍스트가 필요합니다" → db_specialist 로 transfer → SQL 받음 → 다시 query_specialist 로.
- 예: aws_specialist 가 "msk_metric 도구가 없다" → db_specialist 로 transfer (msk_metric 은 db_specialist 가 가짐).

[응답 형태]
- 단순 조회 ('X 보여줘', 'Y 확인해줘') 면 specialist 가 만든 결과를 그대로 또는 한 번 정리해 답변. **묻지 않은 가설·권고·finding ID·다음 확인 항목 같은 정형 보고서를 만들지 마세요.**
- 진단/RCA ('왜 느려?', '원인 분석') 일 때만 여러 specialist 결과를 종합해 발견사항·가설·권고로 정리.

[기타]
- specialist 가 에러를 반환하면 인자/시간 윈도를 한 번 바꾸거나 다른 specialist 를 시도. 사용자에게 "target 을 바꿔주세요" 떠넘기기 금지.
- request 의 lens / targets 는 hint. 비어 있거나 자기 도메인과 어긋나면 무시하고 specialist 의 인프라 컨텍스트로 진행.
- 한 specialist 를 5 회 이상 transfer 했는데 결정적 단서를 못 얻으면 그때까지의 결과로 답하고 종결.
"""


# ─────────────────────── Specialist 빌드 + Supervisor compile ───────────────────────


_GRAPH = None


def _build_graph():
    os_specialist = create_react_agent(
        model=get_llm(),
        tools=OS_TOOLS,
        prompt=SystemMessage(content=_specialist_system("os_specialist", _OS_ROLE)),
        name="os_specialist",
    )
    db_specialist = create_react_agent(
        model=get_llm(),
        tools=DB_TOOLS,
        prompt=SystemMessage(content=_specialist_system("db_specialist", _DB_ROLE)),
        name="db_specialist",
    )
    log_specialist = create_react_agent(
        model=get_llm(),
        tools=LOG_TOOLS,
        prompt=SystemMessage(content=_specialist_system("log_specialist", _LOG_ROLE)),
        name="log_specialist",
    )
    query_specialist = create_react_agent(
        model=get_llm(),
        tools=QUERY_TOOLS,
        prompt=SystemMessage(content=_specialist_system("query_specialist", _QUERY_ROLE)),
        name="query_specialist",
    )
    aws_specialist = create_react_agent(
        model=get_llm(),
        tools=AWS_TOOLS,
        prompt=SystemMessage(content=_specialist_system("aws_specialist", _AWS_ROLE)),
        name="aws_specialist",
    )

    workflow = create_supervisor(
        agents=[os_specialist, db_specialist, log_specialist, query_specialist, aws_specialist],
        model=get_llm(),
        prompt=_SUPERVISOR_PROMPT,
        # full_history: specialist 의 모든 메시지를 supervisor history 에 그대로 노출 →
        # UI 가 specialist 의 도구 호출/결과를 카드로 그릴 수 있고, 다음 specialist 도 history 참조 가능.
        output_mode="full_history",
        supervisor_name="supervisor",
        add_handoff_messages=True,
        add_handoff_back_messages=True,
    )
    return workflow.compile(checkpointer=InMemorySaver())


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


# ─────────────────────── 메시지 정규화 (UI 호환) ───────────────────────


def _flatten_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    txt = c.get("text") or ""
                    if txt:
                        parts.append(txt)
                elif "text" in c and t not in ("tool_use", "tool_result"):
                    parts.append(str(c.get("text")))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _normalize_tool_calls(m: Any) -> list[dict]:
    calls: list[dict] = []
    for tc in (getattr(m, "tool_calls", None) or []):
        calls.append({"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")})
    content = getattr(m, "content", None)
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                calls.append({"id": c.get("id"), "name": c.get("name"), "args": c.get("input")})
    seen: set[str] = set()
    out: list[dict] = []
    for tc in calls:
        key = tc.get("id") or json.dumps(tc, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(tc)
    return out


def normalize_message(m: Any) -> dict:
    role = getattr(m, "type", None) or "ai"
    name = getattr(m, "name", None)
    content = getattr(m, "content", None)
    text = _flatten_text(content)
    tool_calls = _normalize_tool_calls(m)
    out: dict = {
        "role": role,
        "name": name,
        "text": text[:8000] if text else "",
        "tool_calls": tool_calls,
    }
    tcid = getattr(m, "tool_call_id", None)
    if tcid:
        out["tool_call_id"] = tcid
    return out


# ─────────────────────── User message 구성 ───────────────────────


def _format_fast_context(fast: dict[str, Any]) -> str:
    if not fast:
        return ""
    lines: list[str] = ["[1차 fast 분석 결과 — 이미 확보된 정보]"]
    findings = fast.get("findings") or []
    hypotheses = fast.get("hypotheses") or []
    next_actions = fast.get("next_actions") or []
    if findings:
        lines.append(f"\n## findings ({len(findings)}건)")
        for f in findings[:30]:
            sev = (f.get("severity") or "info").upper()
            domain = f.get("domain") or "?"
            fid = f.get("id") or "?"
            title = f.get("title") or ""
            lines.append(f"- [{sev}][{domain}] ({fid}) {title}")
    if hypotheses:
        lines.append(f"\n## hypotheses ({len(hypotheses)}건)")
        for h in hypotheses[:10]:
            conf = h.get("confidence", 0.0) or 0.0
            refs = ", ".join(h.get("supporting_finding_ids") or [])
            lines.append(f"- (conf {conf:.2f}, refs={refs}) {h.get('statement','')}")
    if next_actions:
        lines.append("\n## next_actions")
        for a in next_actions[:10]:
            lines.append(f"- {a}")
    return "\n".join(lines)


def _user_text(request: dict[str, Any]) -> str:
    tr = request.get("time_range") or {}
    fast_block = _format_fast_context(request.get("fast_context") or {})
    head = (
        f"분석 요청: {request.get('free_text','(없음)')}\n"
        f"lens: {request.get('lens','?')}     (hint — 무시 가능)\n"
        f"time_range: {tr.get('start','?')} → {tr.get('end','?')}\n"
        f"targets: {request.get('targets') or '—'}     (hint — 무시 가능)"
    )
    if fast_block:
        return f"{head}\n\n{fast_block}\n\n위 1차 fast 분석은 이미 확보된 컨텍스트입니다. 사용자가 새로 물은 부분에 집중하세요."
    return head


# ─────────────────────── 활성 agent 추출 (subgraphs ns 기반) ───────────────────────

_KNOWN_AGENTS = {"supervisor", "os_specialist", "db_specialist", "log_specialist",
                 "query_specialist", "aws_specialist"}


def _active_from_ns(ns: tuple) -> str:
    """Stream subgraph namespace 에서 활성 agent 이름 추출.

    langgraph supervisor 의 stream(subgraphs=True) 는:
      - outer 노드 (supervisor 라우팅 단계): ns 가 빈 tuple 또는 ('supervisor:...', )
      - specialist 내부 react step: ns 가 ('<specialist>:<id>', ...) 또는 더 깊은 nested
    각 segment 는 'agent_name:checkpoint_id' 형식. ':' 앞 부분에서 알려진 agent 이름이면 그것 반환.
    """
    if not ns:
        return "supervisor"
    # 가장 깊은 segment 부터 (안쪽이 활성)
    for seg in reversed(list(ns)):
        if not isinstance(seg, str):
            continue
        agent_part = seg.split(":", 1)[0]
        if agent_part in _KNOWN_AGENTS:
            return agent_part
    return "supervisor"


# ─────────────────────── 외부 API: iter_swarm / invoke_swarm ───────────────────────


def iter_swarm(request: dict[str, Any], *,
               recursion_limit: int = 50,
               ping_pong_window: int = 6,
               ping_pong_min_unique: int = 2) -> Iterator[dict]:
    """Supervisor 그래프를 stream 모드로 돌리며 의미 있는 이벤트를 yield."""
    yield {"type": "start", "entry": "supervisor",
           "reasoning": "Supervisor 가 사용자 요청을 분석해 적절한 specialist 에게 라우팅합니다."}

    config: dict[str, Any] = {
        "configurable": {"thread_id": request.get("session_id") or "default"},
        "recursion_limit": recursion_limit,
    }
    initial_state: dict[str, Any] = {"messages": [HumanMessage(content=_user_text(request))]}

    handoffs: list[str] = []
    seen_ids: set[str] = set()
    last_active: str | None = None
    n_messages = 0

    try:
        for ns, chunk in _get_graph().stream(
            initial_state,
            config=config,
            stream_mode="values",
            subgraphs=True,
        ):
            active = _active_from_ns(ns)
            if active != last_active:
                last_active = active
                handoffs.append(active)
                yield {"type": "handoff", "agent": active}

            for m in (chunk.get("messages") or []):
                mid = getattr(m, "id", None) or id(m)
                key = str(mid)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                yield {"type": "message", "message": normalize_message(m)}
                n_messages += 1

            # 무한 루프 가드: 같은 specialist 가 계속 호출되거나 supervisor↔A ping-pong
            if len(handoffs) > 30:
                yield {"type": "abort", "reason": "too_many_handoffs"}
                break
    except Exception as e:  # noqa: BLE001
        logger.exception("supervisor stream failed")
        yield {"type": "error", "error": str(e)}
        return

    yield {
        "type": "done",
        "final_active_agent": last_active or "supervisor",
        "handoffs": handoffs,
        "n_messages": n_messages,
    }


def invoke_swarm(request: dict[str, Any], *,
                 recursion_limit: int = 50,
                 ping_pong_window: int = 6,
                 ping_pong_min_unique: int = 2) -> dict[str, Any]:
    """동기 호출 — 모든 이벤트를 모아 최종 결과 dict 반환 (호환용)."""
    messages: list[dict] = []
    handoffs: list[str] = []
    aborted: str | None = None
    final_active: str | None = None
    err: str | None = None

    for ev in iter_swarm(request, recursion_limit=recursion_limit,
                         ping_pong_window=ping_pong_window,
                         ping_pong_min_unique=ping_pong_min_unique):
        t = ev.get("type")
        if t == "message":
            messages.append(ev["message"])
        elif t == "handoff":
            handoffs.append(ev["agent"])
        elif t == "abort":
            aborted = ev.get("reason")
        elif t == "done":
            final_active = ev.get("final_active_agent")
        elif t == "error":
            err = ev.get("error")

    if err:
        return {"error": err, "handoffs": handoffs, "messages": messages}
    return {
        "messages": messages,
        "handoffs": handoffs,
        "final_active_agent": final_active,
        "aborted": aborted,
    }
