"""sql_readonly Lambda — sqlglot AST gate + 실 DB 연결.

env (필수):
  SQL_READONLY_PG_HOST, SQL_READONLY_PG_DBNAME, SQL_READONLY_PG_SECRET_ARN
  SQL_READONLY_MYSQL_HOST, SQL_READONLY_MYSQL_DBNAME, SQL_READONLY_MYSQL_SECRET_ARN

선택:
  SQL_READONLY_MAX_ROWS (default 1000)
  SQL_READONLY_TIMEOUT_MS (default 5000)

입력:  {"engine": "postgres" | "mysql", "db_id": str, "sql": str}
출력:  {"columns": [..], "rows": [[..]], "row_count": int, "validated_sql": str}
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_ROWS = int(os.environ.get("SQL_READONLY_MAX_ROWS", "1000"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("SQL_READONLY_TIMEOUT_MS", "5000"))


@lru_cache(maxsize=8)
def _secret(arn: str) -> dict:
    sm = boto3.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=arn).get("SecretString") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"password": raw}


def _validate(sql: str, dialect: str) -> str:
    import sqlglot
    from sqlglot import exp

    parsed = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(parsed, (exp.Select, exp.Show, exp.Describe)):
        raise ValueError(f"only SELECT/SHOW/DESCRIBE allowed, got {type(parsed).__name__}")
    if isinstance(parsed, exp.Select) and not parsed.args.get("limit"):
        parsed.set("limit", exp.Limit(expression=exp.Literal.number(MAX_ROWS)))
    return parsed.sql(dialect=dialect)


def _run_postgres(sql: str) -> dict[str, Any]:
    import psycopg

    secret_arn = os.environ["SQL_READONLY_PG_SECRET_ARN"]
    creds = _secret(secret_arn)
    dsn = {
        "host":     os.environ["SQL_READONLY_PG_HOST"],
        "port":     int(os.environ.get("SQL_READONLY_PG_PORT", "5432")),
        "dbname":   os.environ.get("SQL_READONLY_PG_DBNAME", "dbaops"),
        "user":     creds.get("username", "dbaops_admin"),
        "password": creds["password"],
        "connect_timeout": 5,
    }
    with psycopg.connect(**dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()] if cur.description else []
    return {"columns": cols, "rows": rows, "row_count": len(rows)}


def _run_mysql(sql: str) -> dict[str, Any]:
    import pymysql

    secret_arn = os.environ["SQL_READONLY_MYSQL_SECRET_ARN"]
    creds = _secret(secret_arn)
    dsn = {
        "host":     os.environ["SQL_READONLY_MYSQL_HOST"],
        "port":     int(os.environ.get("SQL_READONLY_MYSQL_PORT", "3306")),
        "database": os.environ.get("SQL_READONLY_MYSQL_DBNAME", "dbaops"),
        "user":     creds.get("username", "dbaops_admin"),
        "password": creds["password"],
        "connect_timeout": 5,
        "read_timeout": max(STATEMENT_TIMEOUT_MS // 1000, 5),
    }
    conn = pymysql.connect(autocommit=True, **dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME={STATEMENT_TIMEOUT_MS}")
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()] if cur.description else []
        return {"columns": cols, "rows": rows, "row_count": len(rows)}
    finally:
        conn.close()


def _payload(event: dict) -> dict:
    body = event.get("body") if isinstance(event, dict) else None
    if isinstance(body, str):
        return json.loads(body)
    if isinstance(body, dict):
        return body
    return event if isinstance(event, dict) else {}


def handler(event: dict, _ctx) -> dict:
    body = _payload(event)
    engine = body["engine"]
    dialect = "postgres" if engine == "postgres" else "mysql"
    safe_sql = _validate(body["sql"], dialect)
    logger.info("engine=%s sql=%s", engine, safe_sql[:200])

    try:
        result = _run_postgres(safe_sql) if engine == "postgres" else _run_mysql(safe_sql)
    except Exception as e:  # noqa: BLE001
        logger.exception("sql_readonly failed")
        return {"error": str(e)[:500], "validated_sql": safe_sql, "columns": [], "rows": [], "row_count": 0}

    result["validated_sql"] = safe_sql
    return result
