# MCP Tools

AgentCore Gateway 뒤에 Lambda 타겟으로 등록되는 MCP 도구 6종.

| 도구 | 입력 | 출력 (요약 JSON, ≤100 KB) |
|---|---|---|
| `prometheus_query` | promql, range | timeseries [{ts, value}] |
| `cloudwatch_metrics` | namespace, metric, dims, range, stat | timeseries |
| `rds_pi` | db_id, range, group_by | top SQL by AAS |
| `sql_readonly` | sql, db_id, engine | rows (LIMIT 1000), AST 검증 |
| `msk_metrics` | cluster_arn, metric, range | timeseries |
| `s3_log_fetch` | bucket, key, byte_range, regex | matched lines |

각 디렉토리는 `handler.py`, `requirements.txt` (필요 시), `tool_io.json` (Gateway target schema) 를 포함한다.

배포는 Terraform `infra/modules/agentcore` 가 zip 패키징 후 `CreateGatewayTarget` 으로 등록.
