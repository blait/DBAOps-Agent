# MCP Tools

커스텀 MCP 도구 핸들러 4종. `mcp_router`가 직접 import 하여 호출한다.

| 디렉토리 | 도구 | 설명 |
|---|---|---|
| `rds_performance_insights/` | `rds-pi` | RDS PI top SQL by AAS, wait events |
| `msk_metrics/` | `msk-metrics` | MSK/Kafka CloudWatch 메트릭 조회 |
| `s3_log_fetch/` | `s3-log-fetch` | S3 gzip 로그 byte-range + regex |
| `aws_api/` | `aws-api` | RDS/EC2/MSK describe + PI dimension (sub-tool 7개) |

각 디렉토리에 `handler.py` + `tool_io.json`(입출력 스키마). 라우터가 `handler({"body": args, "tool_name": sub}, None)` 으로 호출.

이 외 6종(community-postgres, community-mysql, community-prometheus, awslabs-cloudwatch, awslabs-aws-api, awslabs-aws-doc)은 오픈소스 MCP 서버를 stdio 로 spawn — `mcp_router/stdio_proxy.py` 참조.
