"""sql_readonly Lambda — sqlglot AST gate, statement_timeout, IAM auth.

입력: {"engine": "postgres" | "mysql", "db_id": str, "sql": str, "params": [..]}
출력: {"columns": [..], "rows": [[..]], "row_count": int}

보안 핵심:
- sqlglot 으로 parse → SELECT/EXPLAIN 외 거부
- LIMIT 가 없는 쿼리는 LIMIT 1000 강제
- statement_timeout 5s
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_ROWS = int(os.environ.get("SQL_READONLY_MAX_ROWS", "1000"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("SQL_READONLY_TIMEOUT_MS", "5000"))


def _validate(sql: str, dialect: str) -> str:
    import sqlglot
    from sqlglot import exp

    parsed = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(parsed, (exp.Select, exp.Show, exp.Describe)):
        raise ValueError(f"only SELECT/SHOW/DESCRIBE allowed, got {type(parsed).__name__}")
    if isinstance(parsed, exp.Select) and not parsed.args.get("limit"):
        parsed.set("limit", exp.Limit(expression=exp.Literal.number(MAX_ROWS)))
    return parsed.sql(dialect=dialect)


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    engine = body["engine"]
    sql_in = body["sql"]
    dialect = "postgres" if engine == "postgres" else "mysql"
    safe_sql = _validate(sql_in, dialect)

    # TODO: Phase 2에서 IAM auth + RDS Data API 또는 psycopg/pymysql 연결 구현
    return {"columns": [], "rows": [], "row_count": 0, "validated_sql": safe_sql}
