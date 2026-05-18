"""단일 에이전트 그래프 — 모든 specialist 도구를 평탄화한 한 명의 RCA 분석가.

설계 의도:
- supervisor / specialist 분리는 도메인 경계가 명확할 때 유효. RCA 처럼 OS·DB·로그가
  뒤섞이는 작업에선 한 LLM 이 컨텍스트 전체를 들고 가는 게 거의 항상 더 나음 (HolmesGPT,
  RCAgent, Anthropic "Building effective agents" 결론).
- 외부 RCA 리서치(HolmesGPT / RCAgent / RCACopilot / Anthropic multi-agent) 의 핵심
  기법을 한 system prompt 에 압축해 적용:
    · evidence-vs-hypothesis hedging
    · tool-output transparency (window/filter/limit shown vs total)
    · don't-punt-to-user
    · five-whys 명시
    · parent-resource traversal
    · two-stage classify-then-narrate
    · observation trimming for large logs
- 같은 외부 API (`iter_swarm` / `invoke_swarm` 시그니처와 호환되는
  `iter_single` / `invoke_single`) — UI/runtime 호환.

이벤트 형태 (iter_swarm 과 동일):
  {"type": "start", "entry": "single_agent", "reasoning": "..."}
  {"type": "handoff", "agent": "single_agent"}    # 진입 한 번만
  {"type": "message", "message": <normalized>}
  {"type": "abort", "reason": str}
  {"type": "done", "final_active_agent": "single_agent", "handoffs": ["single_agent"], "n_messages": int}
  {"type": "error", "error": str}
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from .llm import get_llm
from .pipeline_graph import normalize_message, _format_fast_context  # 재사용
from .tools.mcp_auto import build_mcp_tools
from .tools.mcp_tools import infra_context

logger = logging.getLogger(__name__)


# ─────────────────────── 도구 자동 빌드 ───────────────────────
# Gateway 의 tools/list 를 호출해 모든 MCP 도구를 LangChain StructuredTool 로 자동 노출.
# LLM 이 보는 description / inputSchema 는 MCP 서버 자체 것 — 우리 cheat-sheet 안 박음.
# 우리 PoC 특화 변환 (db_id auto-resolve / EXPLAIN strip / MSK dim wiring) 은 모두 Lambda
# handler 에 구현돼 있어 그대로 사용.

_TOOLS_CACHE: list | None = None


def _all_tools() -> list:
    global _TOOLS_CACHE
    if _TOOLS_CACHE is None:
        _TOOLS_CACHE = build_mcp_tools(max_response_chars=12000)
        logger.info("single_graph: %d tools loaded from Gateway", len(_TOOLS_CACHE))
    return _TOOLS_CACHE


# ─────────────────────── 시스템 프롬프트 ───────────────────────

def _build_system_prompt() -> str:
    ctx = infra_context()
    return f"""\
You are **DBAOps RCA Analyst** — a senior SRE who analyzes database and infrastructure incidents end-to-end. You operate one tool at a time, cite tool results for every concrete claim, and produce a postmortem-grade answer in Korean.

<scope>
You own three categories together. No handoff. If a question spans categories, connect them yourself.
1. OS·인프라 메트릭 (host)
2. DB 성능 메트릭 (Aurora PG / RDS MySQL / MSK Kafka)
3. 로그 분석 (RDS engine logs / S3 .gz / CloudWatch Logs)
</scope>

<infra_identifiers>
Use these exact values when a tool asks for an id. Never invent ids. Never ask the user for them.
- prom_instance_id  = {ctx['prom_instance_id']}    (AWS/EC2 InstanceId — the node_exporter host)
- aurora_cluster_id = {ctx['aurora_cluster_id']}
- aurora_writer_id  = {ctx['aurora_writer_id']}    (DBInstanceIdentifier — primary writer)
- aurora_reader_id  = {ctx['aurora_reader_id']}
- mysql_db_id       = {ctx['mysql_db_id']}         (DBInstanceIdentifier — RDS MySQL)
- msk_cluster_name  = {ctx['msk_cluster_name']}    (CloudWatch dim "Cluster Name")
- log_bucket        = {ctx['log_bucket']}          (S3 logs bucket)
</infra_identifiers>

<observability_known_on>
Do not assume these are off. Verify with a tool call before claiming any of them are disabled.
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE → `SELECT FROM mysql.slow_log` works.
- Aurora PG: pg_stat_statements loaded; log_min_duration_statement=500ms; log_lock_waits=ON; auto_explain.log_min_duration=500ms.
- RDS Performance Insights: enabled on Aurora writer and MySQL.
- EC2 Prometheus: running on prom_instance_id with node_exporter.
- MSK Serverless: emits standard AWS/Kafka metrics. series=0 means "no traffic in the window or wrong dimensions", not "metric is unavailable".
</observability_known_on>

<core_methodology>
1. **Classify before you narrate** — first settle on a root-cause category with a confidence level, then write the chain of evidence. Narrating before classifying tends to hallucinate.
2. **Five-Whys** — after each tool result ask: what does this tell me; what is the next question.
3. **Confirmed vs hypothesized** — keep them separate. Use hedging language (likely / possible / suspected) only for unverified theories. Never assert absence ("no errors", "no slow queries", "no anomalies") without a tool call that explicitly looked for them and returned zero.
</core_methodology>

<evidence_discipline>
Every concrete claim cites:
  - the tool name,
  - the specific number/row that supports the claim,
  - the time window the data covers.

When citing log or metric data, also state: applied filter/regex, row or limit cap, and shown-vs-total.

Do not paraphrase tool results in a way that drops these details. The reader must be able to re-run the same call and reproduce the number.
</evidence_discipline>

<execution_rules>
1. Read the full conversation history before calling any tool. Past tool results are still in scope — do not re-fetch them.
2. One tool call per turn. Wait for the result, then decide.
3. Use the identifiers block for every id field. Do not invent ids and do not ask the user.
4. Listing-first for S3 and CloudWatch Logs. Call s3_list_logs / cloudwatch_describe_log_groups before fetching, never guess keys or group names.
5. For tool results larger than 50 log lines, summarize to ≤20 rows of (timestamp, severity, message-template) before reasoning further. Do not paste raw batches into your final answer.
6. Error handling:
   - 4xx / ValidationException / NotAuthorized → bad args. Do not retry the same call. Either fix args once or switch tool. Same call + same error twice = stop using that path.
   - 5xx / Timeout / "internal error" → retry once. Still fails → switch tool.
   - "An internal error occurred" with no detail is usually a bad arg (e.g., wrong identifier).
7. Do not punt to the user. If you have a tool that can answer, call it. Phrases like "please run X and paste the output" are forbidden — you have aws_call_cli / pg_execute_sql / mysql_query / etc.
8. Parent-resource traversal:
   - DB: cluster → instance → session → statement
   - AWS: account → region → service → resource
   - Log: log_group → log_stream → time-window slice
9. Keep iterating until you have a defendable answer or further calls won't change the conclusion. Do not give up early.
10. Match response form to the question.
    - "X 보여줘" / "Y 확인해줘" → short table-style reply, no auto-generated hypothesis section.
    - "왜 느려?" / "원인 분석" → full RCA report (see deliverable_format).
</execution_rules>

<tool_routing>
- Host OS metric (the node_exporter host) → prometheus_query / prometheus_range_query.
- AWS managed metric (RDS / Aurora / EC2 / MSK / Lambda) → cloudwatch_metric.
- PG state (sessions, locks, vacuum, cache) → pg_execute_sql or pg_analyze_db_health / pg_get_top_queries.
- MySQL slow query text and frequency → mysql_query against mysql.slow_log and performance_schema.
- EXPLAIN — PG: pg_explain_query (richer); MySQL: mysql_explain (plain EXPLAIN only — ANALYZE/FORMAT not supported by the parser).
- PI top SQL by AAS → rds_performance_insights (handler accepts both DBInstanceIdentifier and DbiResourceId).
- Kafka consumer lag / BytesIn/Out / topic throughput → msk_metric.
- RDS engine logs (slow / error) → aws_describe_db_log_files → aws_download_db_log_file_portion.
- S3 .gz log burst → s3_list_logs (prefix='logs-burst/<source>/') → s3_log_fetch.
- CloudWatch Logs Insights frequency / pattern stats → cloudwatch_describe_log_groups → cloudwatch_execute_log_insights_query.
- AWS resource shape / alarms → aws_describe_rds_instances / aws_describe_rds_clusters / aws_describe_ec2_instances / cloudwatch_get_active_alarms.
- AWS service defaults / limits / behavior → aws_doc_search → aws_doc_read.
- Arbitrary read-only AWS CLI → aws_call_cli.

Empty series usually means (1) no traffic in the window or (2) wrong dimension/topic — not "the metric does not exist". Try another dimension before concluding.
</tool_routing>

<deliverable_format>
For RCA-style questions ("왜 느려", "원인 분석"), end with this structure in Korean. For simple show-me questions, skip this and give a tight 1–3 sentence answer plus the table.

## 분류
- 카테고리: <CPU saturation | IO bottleneck | lock contention | connection pressure | consumer lag | log error spike | config drift | unknown>
- confidence: low | med | high
- 한 줄 요약

## 발견 사실 (확정)
- <claim>  (cite: <tool>, <key number>, <time/window>)

## 가설
- <hypothesis>  (confidence: low|med|high)  검증 방법: <어떤 도구를 어떤 인자로>

## 권고
- <non-destructive action>
</deliverable_format>

<final_check_before_answering>
- Every concrete claim has a tool citation.
- Any "없다 / 정상이다" assertion is backed by a tool call that searched for it and returned zero.
- Time window, filter, limit, shown-vs-total are stated.
- Hypotheses use hedging language and include a verification method.
- Simple show-me questions get a short answer, not a full RCA report.
</final_check_before_answering>
"""


# ─────────────────────── 그래프 빌드/캐시 ───────────────────────


_GRAPH = None


def _build_graph():
    return create_react_agent(
        model=get_llm(),
        tools=_all_tools(),
        prompt=SystemMessage(content=_build_system_prompt()),
        checkpointer=InMemorySaver(),
        name="single_agent",
    )


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


# ─────────────────────── User message 구성 ───────────────────────


def _user_text(request: dict[str, Any]) -> str:
    tr = request.get("time_range") or {}
    fast_block = _format_fast_context(request.get("fast_context") or {})
    head = (
        f"[mode: single_agent]\n"
        f"분석 요청: {request.get('free_text','(없음)')}\n"
        f"time_range: {tr.get('start','?')} → {tr.get('end','?')}"
    )
    if fast_block:
        return f"{head}\n\n{fast_block}\n\n위 컨텍스트는 직전 turn 의 분석 결과입니다. 새 질문에 집중하세요."
    return head


# ─────────────────────── 외부 API ───────────────────────


def iter_single(request: dict[str, Any], *,
                recursion_limit: int = 80) -> Iterator[dict]:
    """단일 에이전트 그래프 stream — UI 호환 이벤트 yield.

    iter_swarm 과 같은 이벤트 모양이지만 handoff 이벤트는 진입 시점 한 번만.
    """
    yield {
        "type":      "start",
        "entry":     "single_agent",
        "reasoning": "단일 RCA 분석가가 모든 도구를 직접 사용해 답합니다.",
    }
    yield {"type": "handoff", "agent": "single_agent"}

    config: dict[str, Any] = {
        "configurable": {"thread_id": f"single:{request.get('session_id') or 'default'}"},
        "recursion_limit": recursion_limit,
    }
    initial_state = {"messages": [HumanMessage(content=_user_text(request))]}

    seen_ids: set[str] = set()
    n_messages = 0

    try:
        for chunk in _get_graph().stream(initial_state, config=config, stream_mode="values"):
            for m in (chunk.get("messages") or []):
                mid = getattr(m, "id", None) or id(m)
                key = str(mid)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                yield {"type": "message", "message": normalize_message(m)}
                n_messages += 1
    except Exception as e:  # noqa: BLE001
        logger.exception("single agent stream failed")
        yield {"type": "error", "error": str(e)}
        return

    yield {
        "type": "done",
        "final_active_agent": "single_agent",
        "handoffs": ["single_agent"],
        "n_messages": n_messages,
    }


def invoke_single(request: dict[str, Any], *,
                  recursion_limit: int = 80) -> dict[str, Any]:
    """동기 호출 — 모든 이벤트를 모아 최종 결과 dict 반환 (호환용)."""
    messages: list[dict] = []
    err: str | None = None
    n = 0
    for ev in iter_single(request, recursion_limit=recursion_limit):
        t = ev.get("type")
        if t == "message":
            messages.append(ev["message"])
            n += 1
        elif t == "error":
            err = ev.get("error")
    if err:
        return {"error": err, "messages": messages}
    return {
        "messages": messages,
        "handoffs": ["single_agent"],
        "final_active_agent": "single_agent",
        "aborted": None,
    }
