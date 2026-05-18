"""3 Supervisor 패턴 (langgraph-supervisor 기반).

엔터프라이즈 분석 카탈로그 정의에 맞춰 supervisor 3명을 둔다 — 각자 책임/입력/도구·소스/산출물
형식이 명확히 다르다. specialist 6명은 모든 supervisor 가 공유하지만, supervisor 별로 라우팅 가능한
specialist 부분집합이 다르다.

  - os_metric : OS·인프라 메트릭 분석 (Prometheus + CloudWatch — 메트릭 추세/이상/가설)
  - db_metric : DB·Kafka 성능 메트릭 분석 (pg_stat_*, Performance Schema, JMX, PI — TPS/QPS/Lock/Lag)
  - log       : 로그 분석 (S3 .gz, RDS 엔진 로그, CloudWatch Logs — 에러 분류/빈발 패턴/RCA)

사용자는 UI 탭에서 supervisor 를 선택하고, 요청은 `request["supervisor"]` 에 그 이름이 담겨 들어온다.
supervisor 별로 산출물 contract 가 시스템 프롬프트에 명시돼 있어, 응답이 카테고리에 맞는 형식으로 나온다.

이벤트 형태 (변경 없음 — UI 호환):
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
    DOCS_TOOLS,
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
0. **첫 행동: history 검토** — 도구 호출하기 전에 항상 message history 를 위에서 아래로 읽으세요.
   - supervisor 가 transfer 시 적은 `[Supervisor → <name>] Task / Context / Verify` 블록이 있으면 **그것이 당신의 우선 지시문**. 자기 system prompt 보다 우선.
   - 사용자 원본 메시지 (history 첫 user message) 가 진짜 의도. supervisor 의 task 가 사용자 의도와 어긋나 보이면 사용자 의도 우선.
   - 이전 specialist 가 이미 확보한 데이터 (도구 결과, SQL 텍스트, 메트릭 값) 가 있으면 같은 도구를 재호출하지 말고 그 결과를 인용. 예: "db_specialist 가 직전에 mysql.slow_log 5건 가져왔으니 그 SQL 로 EXPLAIN 만 돌리겠습니다."
1. 도구 호출 → 결과 확인 → 다음 결정. **한 턴에 도구 호출은 1 개**. 병렬/연쇄 호출 시 일부 호출에서 필수 인자가 누락되는 패턴이 자주 발견됨. 도구 schema 의 required 필드는 모두 채우고, 인스턴스 ID 같은 고정값은 위 [인프라 식별자] 블록의 값을 인자에 그대로 사용하세요.
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
- `prometheus_query(query, time?)`: Prometheus instant query.
- `prometheus_range_query(query, start, end, step='30s')`: 시계열 추세.
    · CPU 사용률: `100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100`
    · 메모리 사용 바이트: `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
    · 디스크 IO: `rate(node_disk_io_time_seconds_total[5m])`
    · 네트워크 RX: `rate(node_network_receive_bytes_total[5m])`
- `cloudwatch_metric(namespace, metric_name, start_time, end_time, dimensions=[...], statistic='Average', period=60)`:
    · EC2: namespace='AWS/EC2', dimensions=[{'Name':'InstanceId','Value': prom_instance_id}]
    · RDS: namespace='AWS/RDS', dimensions=[{'Name':'DBInstanceIdentifier','Value': aurora_writer_id 또는 mysql_db_id}]
"""


_DB_ROLE = """\
[전문 분야]
PostgreSQL / MySQL / Kafka 내부 성능 분석. pg_stat_*, performance_schema, mysql.slow_log, RDS Performance Insights, MSK CloudWatch.

[관측 인프라 사실 — 모두 켜져 있음. OFF 라고 가정하지 마세요]
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE, log_queries_not_using_indexes=ON
- Aurora PG: pg_stat_statements 로드, log_min_duration_statement=500ms, log_lock_waits=ON, auto_explain.log_min_duration=500ms
모든 통계 테이블에 데이터가 채워지므로 "관측 결함이라 분석 불가" 같은 추측 단언 금지.

[핵심 도구 사용 매뉴얼]
**MySQL — 가장 빠른 답** (mcp-server-mysql, RO default):
  `mysql_query(sql="SELECT start_time, query_time, lock_time, rows_examined, LEFT(CONVERT(sql_text USING utf8), 200) AS sql_text FROM mysql.slow_log WHERE start_time > NOW() - INTERVAL 30 MINUTE ORDER BY query_time DESC LIMIT 10")`
  `mysql_query(sql="SELECT count_star, ROUND(sum_timer_wait/1e9,1) AS sum_ms, LEFT(digest_text, 200) AS digest FROM performance_schema.events_statements_summary_by_digest WHERE digest_text LIKE '%dbaops%' ORDER BY sum_timer_wait DESC LIMIT 10")`
  `mysql_query(sql="SELECT * FROM performance_schema.data_lock_waits LIMIT 50")`

**PG 활성 세션·락·헬스** (postgres-mcp restricted RO):
  `pg_execute_sql(sql="SELECT pid, state, wait_event_type, wait_event, query FROM pg_stat_activity WHERE state != 'idle'")`
  `pg_get_top_queries(sort_by='resources', limit=10)` ← pg_stat_statements 기반
  `pg_analyze_db_health(health_type='all')` ← buffer/connection/vacuum/replication 종합
  `pg_execute_sql(sql="SELECT * FROM pg_locks WHERE NOT granted")`

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

**CloudWatch Logs Insights** (awslabs cloudwatch-mcp):
  1) `cloudwatch_describe_log_groups(log_group_name_prefix='/dbaops/poc/')` 로 log group 후보 발견.
  2) `cloudwatch_execute_log_insights_query(log_group_names=[...], query_string="fields @timestamp, @message | filter @message like /ERROR/ | stats count() by bin(1m)", start_time, end_time)` — 패턴/빈도 강력.
"""


_QUERY_ROLE = """\
[전문 분야]
EXPLAIN [ANALYZE] 결과 해석 + 인덱스/리라이팅/힌트 권고. 풀스캔, Nested Loop, Sort, Hash join, 임시 테이블 식별.

[핵심 도구 사용 매뉴얼]
받은 요청에 SQL 텍스트가 명시돼 있어야 합니다. 없으면 작동 규칙 #10 에 따라 한 줄 거절 후 종결하세요 (supervisor 가 db_specialist 로 다시 라우팅).

**PG** (postgres-mcp): `pg_explain_query(sql='<SELECT>', hypothetical_indexes=[...]?)` — actual time, Rows Removed by Filter, Sort 메모리, Buffers, **가상 인덱스 시뮬레이션**.
  `pg_analyze_workload_indexes(max_index_size_mb=10000)` — 워크로드 분석 후 추가 인덱스 권고.

**MySQL** (mcp-server-mysql): `mysql_explain(sql='<SELECT>', analyze=True)` — type=ALL, Using temporary/filesort, hash join.

**인덱스 메타**:
  PG: `pg_execute_sql(sql="SELECT * FROM pg_indexes WHERE tablename='<t>'")`
  MySQL: `mysql_query(sql="SELECT * FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_NAME='<t>'")`
"""


_DOCS_ROLE = """\
[전문 분야]
AWS 공식 문서 검색/조회 (awslabs aws-documentation-mcp). 사용자가 AWS 서비스의 동작·default·한도·용어 같은 사실 확인을 요청할 때.

[핵심 도구 사용 매뉴얼]
- `aws_doc_search(search_phrase, limit=10)`: 검색어로 docs 페이지 목록 (URL + 요약).
- `aws_doc_read(url, max_length, start_index)`: 특정 docs URL 의 본문을 markdown 으로. 길면 start_index 로 페이지네이션.
- `aws_doc_recommend(url)`: 그 페이지 관련 추천 페이지.

[작업 패턴]
1) `aws_doc_search` 로 후보 URL 1~3개 확보 (정확한 검색어가 핵심 — 'Aurora PostgreSQL max_connections default' 같이 구체적으로).
2) 가장 적합한 URL 을 `aws_doc_read` 로 읽음.
3) 사용자 질문에 직접 답하는 부분만 인용해 짧게 답변.
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
- `cloudwatch_get_active_alarms(max_items?)`: 현재 ALARM 상태 알람 목록 (awslabs cloudwatch-mcp).
- `cloudwatch_get_alarm_history(alarm_name, max_items?)`: 알람 상태 전이 이력.
- `aws_describe_db_log_files(db_instance_identifier, filename_contains?)`: 로그 파일 목록 (다운로드는 log_specialist).
- `aws_describe_pi_dimensions(dbi_resource_id, group_by?, start?, end?)`: group_by prefix — `db.sql_tokenized` (default) | `db.wait_event` | `db.host` | `db.user`.
- `aws_call_cli(cli_command)`: 임의 read-only AWS CLI 명령 실행 (awslabs aws-api-mcp). 'aws sts get-caller-identity' 같은 우리 PoC 도구가 안 만든 API fallback.
- `aws_suggest_cli(query)`: 자연어 → AWS CLI 명령 추천.
"""


# ─────────────────────── Supervisor 시스템 프롬프트 ───────────────────────
#
# 3 supervisor — 각자 카테고리 책임/입력/도구·소스/산출물 형식이 다르다.
# specialist 6명은 모두에게 노출되지만, 각 supervisor 의 프롬프트가
# (a) 자기 카테고리 도메인만 다루고
# (b) 산출물 contract 를 자기 형식으로 강제
# 하도록 명시한다.

# 공통 transfer 의무 + 거버넌스 룰 (3 supervisor 가 모두 공유)
_SUPERVISOR_COMMON_RULES = """\
[Transfer 의무 — 매우 중요]
specialist 에게 transfer 할 때 transfer_to_<spec> 도구의 세 인자를 모두 채우세요. 이 셋이 specialist 가 진입 시점에 보는 explicit 지시문이 됩니다.
- task    : 이 specialist 가 즉시 수행할 구체적 작업 한 문장
- context : history 에서 이미 확보된 사실 한 문장 (처음 transfer 면 사용자 원본 메시지 요약)
- verify  : supervisor 로 복귀할 때 답해야 할 것 한 문장
빈 인자로 transfer 호출 금지.

[자가 교정 — 잘못 라우팅한 경우]
specialist 가 "제 영역이 아닙니다 / 입력이 부족합니다 / 도구셋에 없습니다" 라고 응답하면, 위 도구 카탈로그를 다시 보고 실제로 그 도구를 가진 specialist 로 다시 transfer 하세요. 사용자에게 "도구가 없다" 고 떠넘기지 마세요.

[증거-가설 hedging 룰 — 모든 산출물에 적용]
- 도구 결과로 직접 확인된 사실 = 단언 + 도구명·수치·시점 인용 (예: "P95 RDS CPU = 92% (cloudwatch_metric, 14:02-14:07)")
- 미확인 추정 = 'likely / possible / 추정' 어휘 + confidence (low/med/high)
- 도구 호출 없이 "X 가 없다 / 정상이다" 단언 금지. describe/list/range 호출로 확인 후에만 단언.

[Tool 출력 투명성]
log/metric 결과를 인용할 때 항상 명시: 시간 윈도, 적용 필터/regex, limit, shown vs total.

[Don't punt to user]
당신의 specialist 풀에 도구가 있으면 직접 호출하세요. "사용자께서 X 를 실행해 paste 해주세요" 금지.

[scope boundary — 카테고리 밖 요청]
사용자 요청이 이 supervisor 의 카테고리 밖이면 (예: log supervisor 에 EXPLAIN 요청) 한 줄로 안내:
"이 요청은 <other_supervisor> 카테고리입니다. UI 탭에서 해당 supervisor 로 다시 요청해 주세요." 후 종결.

[종료 조건]
한 specialist 를 5 회 이상 transfer 했는데 결정적 단서가 없으면 그때까지의 결과로 산출물 형식대로 답하고 종결.
"""


# ─── 1. OS·인프라 메트릭 분석 supervisor ───
_SUPERVISOR_OS_METRIC = f"""\
당신은 **OS·인프라 메트릭 분석** supervisor 입니다.

<core_responsibility>
OS·호스트 레이어 메트릭 (CPU/메모리/디스크/네트워크) 의 추세, 이상 탐지, 임계치 도달 시점 분석.
</core_responsibility>

<expected_inputs>
- 시간 범위 (time_range)
- 대상 인스턴스 (targets — EC2 InstanceId / RDS DBInstanceIdentifier)
- 분석 관점 (예: peak 탐지, baseline 대비, 디스크 IO 추세)
사용자 요청에 위 입력 중 빠진 게 있으면 인프라 컨텍스트의 기본값을 사용 (prom_instance_id / aurora_writer_id 등).
</expected_inputs>

<tools_and_sources>
Prometheus (Node Exporter), AWS CloudWatch — top/iostat/vmstat 영역. **DBMS 내부 카운터·로그 분석은 다른 supervisor 영역.**
</tools_and_sources>

<routing_specialists>
6명 specialist 풀 전체에 접근 가능. 단, 카테고리 책임에 맞춰 우선순위가 다릅니다.

[주력 — 거의 매 분석에서 사용]
- **os_specialist** : prometheus_query / prometheus_range_query, cloudwatch_metric (AWS/EC2, AWS/RDS host-level)
- **aws_specialist** : aws_describe_ec2_instances, aws_describe_rds_instances (인스턴스 형상/클래스/AZ)

[보조 — 필요 시]
- **db_specialist** : RDS engine-level CW 메트릭이 host 메트릭과 상관 분석할 때 (예: CPU↑ ↔ DatabaseConnections↑)
- **log_specialist** : 호스트 OS 이벤트 로그 (CloudWatch Logs, /var/log) 가 메트릭 이상 지점과 시간대 상관일 때
- **docs_specialist** : EC2 인스턴스 클래스 default 사양·CloudWatch 메트릭 default 등 사실 확인이 RCA 의 핵심일 때

[교차 도메인]
- **query_specialist** : 이 카테고리에선 거의 안 씀. SQL 텍스트가 RCA 핵심이면 사용자에게 DB 성능 supervisor 안내.
</routing_specialists>

<deliverable_format>
산출물은 반드시 아래 3 섹션으로:

## 메트릭 추세
- <metric>: <window> p50=<v> / p95=<v> / peak=<v>@<ts>  (도구 인용)
- ...

## 이상 지점
- <ts> | <metric> | observed=<v> vs baseline=<v> (Δ +xx%)  (도구 인용)
- 이상 없음이면 "윈도 내 임계 초과 없음" 한 줄 + 사용한 임계 명시.

## 가설
- <one-line hypothesis> · confidence: low|med|high · 검증 방법: <도구+인자>
- ...
</deliverable_format>

<scope_boundary>
- DBMS 내부 카운터 (TPS/QPS/Lock/Cache hit/Lag/ISR) 요청 → "DB 성능 메트릭 분석" supervisor 로 안내
- 로그 패턴 분류 / RCA → "로그 분석" supervisor 로 안내
</scope_boundary>

{_SUPERVISOR_COMMON_RULES}
"""


# ─── 2. DB·Kafka 성능 메트릭 분석 supervisor ───
_SUPERVISOR_DB_METRIC = f"""\
당신은 **DB 성능 메트릭 분석** supervisor 입니다.

<core_responsibility>
DBMS·Kafka 클러스터 내부 성능 메트릭 정량 분석 — TPS·QPS·Lock·Cache hit·Lag·ISR 추세, 비정상 패턴 탐지.
</core_responsibility>

<expected_inputs>
- 대상 시스템 (PG / MySQL / Kafka)
- 시간 범위
- 분석 관점 (예: TOP 쿼리, 락 경합, lag 추세, 커넥션 풀 포화)
</expected_inputs>

<tools_and_sources>
- MySQL: performance_schema, mysql.slow_log, INFORMATION_SCHEMA
- PostgreSQL: pg_stat_statements, pg_stat_activity, pg_locks
- Kafka: JMX → CloudWatch (BytesIn/Out, MessagesIn, MaxOffsetLag, SumOffsetLag, UnderReplicatedPartitions, EstimatedMaxTimeLag)
- RDS Performance Insights
- ※ MS-SQL DMV / ClickHouse system.metrics 는 현재 PoC 인프라 미지원 — 요청 시 미지원 안내.

[관측 인프라 사실 — 모두 켜져 있음]
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE, log_queries_not_using_indexes=ON
- Aurora PG: pg_stat_statements 로드, log_min_duration_statement=500ms, log_lock_waits=ON
"OFF 라 분석 불가" 가정 금지.
</tools_and_sources>

<routing_specialists>
6명 specialist 풀 전체에 접근 가능. 단, 카테고리 책임에 맞춰 우선순위가 다릅니다.

[주력 — 거의 매 분석에서 사용]
- **db_specialist** : pg_*, mysql_*, rds_performance_insights, msk_metric, cloudwatch_metric (RDS engine-level)
- **query_specialist** : pg_explain_query, pg_analyze_workload_indexes, mysql_explain — 특정 SQL 의 실행계획·인덱스 권고가 RCA 의 핵심일 때
- **aws_specialist** : aws_describe_rds_* / aws_describe_pi_dimensions (DbiResourceId 확보 등 메타데이터)

[보조 — 필요 시]
- **os_specialist** : DB 메트릭이 호스트 자원(CPU/IO/메모리) 한계와 상관일 때
- **log_specialist** : 메트릭 이상이 RDS 엔진 로그(slow/error) 패턴과 시간대 일치할 때
- **docs_specialist** : Aurora PG / MySQL / Kafka 의 default 한도·동작이 RCA 의 핵심일 때 (예: max_connections default)
</routing_specialists>

<deliverable_format>
산출물은 반드시 아래 3 섹션으로:

## 추세 (TPS·QPS·Lock·Cache hit·Lag·ISR)
- <metric>: <window> 동안 <trend> (peak=<v>@<ts>)  (도구 인용)
- ...

## 비정상 패턴
- <pattern> | evidence=<도구+수치+시점>
- 패턴 없음이면 "윈도 내 비정상 패턴 미탐지" + 사용한 임계 명시.

## 가설
- <one-line hypothesis> · confidence: low|med|high · 검증 방법: <도구+인자>
- ...
</deliverable_format>

<scope_boundary>
- 호스트 CPU/메모리/디스크 IO → "OS·인프라 메트릭 분석" supervisor 로 안내
- 로그 본문 패턴 분류 → "로그 분석" supervisor 로 안내
</scope_boundary>

{_SUPERVISOR_COMMON_RULES}
"""


# ─── 3. 로그 분석 supervisor ───
_SUPERVISOR_LOG = f"""\
당신은 **로그 분석** supervisor 입니다.

<core_responsibility>
Error / Slow / Audit / 시스템 로그의 패턴 분류, 빈발 에러 탐지, RCA 후보 도출.
</core_responsibility>

<expected_inputs>
- 로그 소스 (postgres / mysql / kafka / RDS engine slow|error)
- 시간 범위
- 키워드 / regex
</expected_inputs>

<tools_and_sources>
- S3 로그: postgresql.log, MySQL error/slow, Kafka server.log / connect.log / ksql log (.gz)
- RDS 엔진 로그: aws_describe_db_log_files / aws_download_db_log_file_portion
- CloudWatch Logs Insights: 빈도·패턴 stats by bin
- ※ Oracle alert.log / SQL Server ERRORLOG 는 현재 PoC 인프라 미지원 — 요청 시 미지원 안내.
</tools_and_sources>

<observation_trimming>
raw 로그를 50 줄 초과해 산출물에 그대로 paste 금지. 항상 ≤20 events 로 (timestamp + severity + 메시지 요약) 표 형식으로 압축한 뒤 분석.
</observation_trimming>

<routing_specialists>
6명 specialist 풀 전체에 접근 가능. 단, 카테고리 책임에 맞춰 우선순위가 다릅니다.

[주력 — 거의 매 분석에서 사용]
- **log_specialist** : s3_list_logs, s3_log_fetch, aws_describe_db_log_files, aws_download_db_log_file_portion, cloudwatch_execute_log_insights_query
- **aws_specialist** : aws_describe_rds_* (인스턴스 식별 및 로그 파일 목록 조회 보조)

[보조 — 필요 시]
- **db_specialist** : 로그에서 발견한 패턴(예: "deadlock") 의 빈도·발생 지표를 performance_schema / pg_stat_* 로 교차검증할 때
- **query_specialist** : 로그에 나타난 슬로우 SQL 텍스트의 EXPLAIN 이 RCA 결정타일 때
- **os_specialist** : 로그 burst 시점이 호스트 자원 spike 와 시간대 상관일 때
- **docs_specialist** : 로그 메시지의 의미·복구 절차가 AWS 공식 문서에 명시된 경우
</routing_specialists>

<deliverable_format>
산출물은 반드시 아래 4 섹션으로:

## 에러 분류
- <class>: <count>건 (sample: "<message>")  (도구 인용 + 시간 윈도 + 필터 명시)
- ...

## 빈발 패턴
- <pattern> @ <window> · <count>회 · 빈도 추세 (예: 14:00-14:30 sec당 3건 → 14:30-15:00 sec당 12건)
- ...

## RCA 후보
- <candidate root cause> ← <evidence chain — 도구+로그 인용>
- 미정이면 "결정적 단서 없음. 추가 확인 필요 항목으로 이동" 한 줄.

## 추가 확인 필요 항목
- <확인 대상> via <도구·인자> (다른 supervisor 영역이면 어느 supervisor 인지 명시)
- ...
</deliverable_format>

<scope_boundary>
- 메트릭 추세 분석 → "OS·인프라 메트릭" 또는 "DB 성능 메트릭" supervisor 로 안내
- EXPLAIN / 인덱스 권고 → "DB 성능 메트릭" supervisor (query_specialist 보유)
</scope_boundary>

{_SUPERVISOR_COMMON_RULES}
"""


# supervisor key → (prompt, agents). 모든 supervisor 가 6 specialist 풀 전체 접근.
# 카테고리 우선순위는 system prompt 의 routing_specialists 섹션에서 강제.
_ALL_SPECIALISTS = [
    "os_specialist", "db_specialist", "log_specialist",
    "query_specialist", "aws_specialist", "docs_specialist",
]

_SUPERVISOR_REGISTRY: dict[str, dict] = {
    "os_metric": {
        "prompt":     _SUPERVISOR_OS_METRIC,
        "agents":     _ALL_SPECIALISTS,
        "label":      "OS·인프라 메트릭 분석",
    },
    "db_metric": {
        "prompt":     _SUPERVISOR_DB_METRIC,
        "agents":     _ALL_SPECIALISTS,
        "label":      "DB 성능 메트릭 분석",
    },
    "log": {
        "prompt":     _SUPERVISOR_LOG,
        "agents":     _ALL_SPECIALISTS,
        "label":      "로그 분석",
    },
}


def supervisor_keys() -> list[str]:
    return list(_SUPERVISOR_REGISTRY.keys())


def supervisor_label(key: str) -> str:
    return _SUPERVISOR_REGISTRY.get(key, {}).get("label", key)


# ─────────────────────── Custom transfer tool with explicit task message ───────────────────────


from typing import Annotated, cast
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from langgraph_supervisor.handoff import METADATA_KEY_HANDOFF_DESTINATION


def _make_explicit_transfer_tool(agent_name: str):
    """supervisor 가 specialist 에 transfer 할 때 task / context / verify 셋을 명시적으로
    적도록 강제하는 custom handoff 도구.

    langgraph-supervisor 의 default `create_handoff_tool` 은 args 없이 단순 transfer 라
    specialist 가 받는 의도가 빈약함. 이 도구는 supervisor 가 LLM 으로서 transfer 직전에
    실제 task 텍스트를 작성하게 만들어 history 에 explicit 메시지로 박는다.
    """
    tool_name = f"transfer_to_{agent_name}"

    @tool(
        tool_name,
        description=(
            f"Transfer control to {agent_name} with an explicit task assignment. "
            "You MUST fill all three fields: task (one sentence — concrete action this specialist should perform), "
            "context (one sentence — what's already known from message history), "
            "verify (one sentence — what this specialist must report back)."
        ),
    )
    def handoff_with_task(
        task: str,
        context: str,
        verify: str,
        state: Annotated[dict, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        # specialist 가 진입 시 반드시 보게 될 explicit instruction
        instruction = (
            f"[Supervisor → {agent_name}]\n"
            f"  Task   : {task}\n"
            f"  Context: {context}\n"
            f"  Verify : {verify}"
        )
        tool_msg = ToolMessage(
            content=f"Successfully transferred to {agent_name}\n\n{instruction}",
            name=tool_name,
            tool_call_id=tool_call_id,
            response_metadata={METADATA_KEY_HANDOFF_DESTINATION: agent_name},
        )
        last_ai_message = cast(AIMessage, state["messages"][-1])
        # 병렬 handoff 케이스 처리 (langgraph-supervisor default 와 동일)
        if len(last_ai_message.tool_calls) > 1:
            handoff_messages = state["messages"][:-1]
            handoff_messages.append(
                AIMessage(
                    content=last_ai_message.content,
                    tool_calls=[
                        tc for tc in last_ai_message.tool_calls if tc["id"] == tool_call_id
                    ],
                    name=last_ai_message.name,
                )
            )
        else:
            handoff_messages = state["messages"]
        handoff_messages = handoff_messages + [tool_msg]
        return Command(
            goto=agent_name,
            graph=Command.PARENT,
            update={"messages": handoff_messages},
        )

    return handoff_with_task


# ─────────────────────── Specialist 빌드 + Supervisor compile ───────────────────────


_GRAPHS: dict[str, Any] = {}


_DEFAULT_SUPERVISOR_KEY = "db_metric"


def _resolve_supervisor_key(key: str | None) -> str:
    if key and key in _SUPERVISOR_REGISTRY:
        return key
    return _DEFAULT_SUPERVISOR_KEY


def _build_specialist(name: str, role_text: str, tools: list):
    return create_react_agent(
        model=get_llm(),
        tools=tools,
        prompt=SystemMessage(content=_specialist_system(name, role_text)),
        name=name,
    )


def _build_graph(supervisor_key: str):
    """supervisor_key 별로 별도 그래프 — 각자 다른 system prompt + 다른 specialist 부분집합."""
    spec_factories = {
        "os_specialist":    lambda: _build_specialist("os_specialist",    _OS_ROLE,    OS_TOOLS),
        "db_specialist":    lambda: _build_specialist("db_specialist",    _DB_ROLE,    DB_TOOLS),
        "log_specialist":   lambda: _build_specialist("log_specialist",   _LOG_ROLE,   LOG_TOOLS),
        "query_specialist": lambda: _build_specialist("query_specialist", _QUERY_ROLE, QUERY_TOOLS),
        "aws_specialist":   lambda: _build_specialist("aws_specialist",   _AWS_ROLE,   AWS_TOOLS),
        "docs_specialist":  lambda: _build_specialist("docs_specialist",  _DOCS_ROLE,  DOCS_TOOLS),
    }

    cfg = _SUPERVISOR_REGISTRY[supervisor_key]
    allowed_names: list[str] = cfg["agents"]
    agents = [spec_factories[n]() for n in allowed_names]
    handoff_tools = [_make_explicit_transfer_tool(n) for n in allowed_names]

    workflow = create_supervisor(
        agents=agents,
        model=get_llm(),
        prompt=cfg["prompt"],
        tools=handoff_tools,
        output_mode="full_history",
        supervisor_name="supervisor",
        add_handoff_messages=True,
        add_handoff_back_messages=True,
    )
    return workflow.compile(checkpointer=InMemorySaver())


def _get_graph(supervisor_key: str | None = None):
    key = _resolve_supervisor_key(supervisor_key)
    if key not in _GRAPHS:
        _GRAPHS[key] = _build_graph(key)
    return _GRAPHS[key]


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
    sup_key = _resolve_supervisor_key(request.get("supervisor"))
    sup_label = supervisor_label(sup_key)
    perspective = request.get("perspective") or request.get("lens") or "?"
    head = (
        f"[카테고리: {sup_label}]\n"
        f"분석 요청: {request.get('free_text','(없음)')}\n"
        f"분석 관점: {perspective}\n"
        f"time_range: {tr.get('start','?')} → {tr.get('end','?')}"
    )
    if fast_block:
        return f"{head}\n\n{fast_block}\n\n위 1차 fast 분석은 이미 확보된 컨텍스트입니다. 사용자가 새로 물은 부분에 집중하세요."
    return head


# ─────────────────────── 활성 agent 추출 (subgraphs ns 기반) ───────────────────────

_KNOWN_AGENTS = {"supervisor", "os_specialist", "db_specialist", "log_specialist",
                 "query_specialist", "aws_specialist", "docs_specialist"}


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
    """Supervisor 그래프를 stream 모드로 돌리며 의미 있는 이벤트를 yield.

    request["supervisor"] 로 3 supervisor 중 어느 그래프를 쓸지 선택. 미지정 시 default.
    """
    sup_key = _resolve_supervisor_key(request.get("supervisor"))
    sup_label = supervisor_label(sup_key)
    yield {
        "type":      "start",
        "entry":     "supervisor",
        "supervisor": sup_key,
        "reasoning": f"[{sup_label}] supervisor 가 사용자 요청을 분석해 적절한 specialist 에게 라우팅합니다.",
    }

    config: dict[str, Any] = {
        "configurable": {"thread_id": f"{sup_key}:{request.get('session_id') or 'default'}"},
        "recursion_limit": recursion_limit,
    }
    initial_state: dict[str, Any] = {"messages": [HumanMessage(content=_user_text(request))]}

    handoffs: list[str] = []
    seen_ids: set[str] = set()
    last_active: str | None = None
    n_messages = 0

    try:
        for ns, chunk in _get_graph(sup_key).stream(
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
