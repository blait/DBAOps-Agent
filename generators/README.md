# Generators

ECS Fargate Spot 에서 실행되는 데이터·로그 생성기.

## data_generator/

| 패턴 | 스케줄 | 대상 | 효과 |
|---|---|---|---|
| `baseline` | always-on | PG/MySQL/Kafka | PG 50 TPS / MySQL 30 QPS / Kafka 100 msg/s |
| `lock_contention` | 30분마다 3분 | PG | hot row `SELECT … FOR UPDATE` 동시 |
| `slow_query` | 20분마다 2분 | MySQL | 인덱스 누락 풀스캔 조인 |
| `connection_spike` | 45분마다 90초 | PG | 10초간 200 short conn |
| `kafka_isr_shrink` | 60분마다 60초 | MSK | producer batch jump + consumer pause |

EventBridge Scheduler → ECS RunTask. Spot 중단 OK (idempotent).

## log_generator/

DB/Kafka 로그 라인을 baseline 1 line/s + EventBridge burst (200 lines/min × 2분) 으로 생성. S3 + CW Logs 양쪽으로 출력.

- PG: `ERROR: deadlock detected`, `LOG: duration: ... ms statement: …`, `FATAL: too many connections`
- MySQL: error log `[ERROR]`, slow log w/ Query_time + Lock_time, audit log JSON
- Kafka: `server.log` ISR shrink, `connect.log` task failure, `ksql` query restart
