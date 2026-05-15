"""rds_pi Lambda — Performance Insights top SQL by AAS.

입력: {"db_id", "start", "end", "group_by": "db.sql_tokenized.statement"}
출력: {"top_sql": [{"statement", "aas"}]}
"""

from __future__ import annotations

import json
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

pi = boto3.client("pi")


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    resp = pi.get_resource_metrics(
        ServiceType="RDS",
        Identifier=body["db_id"],
        MetricQueries=[
            {
                "Metric": "db.load.avg",
                "GroupBy": {"Group": body.get("group_by", "db.sql_tokenized.statement"), "Limit": 10},
            }
        ],
        StartTime=body["start"],
        EndTime=body["end"],
        PeriodInSeconds=60,
    )
    top: list[dict] = []
    for s in resp.get("MetricList", []):
        dims = s.get("Key", {}).get("Dimensions", {})
        values = s.get("DataPoints") or []
        avg = sum(p["Value"] for p in values) / max(len(values), 1)
        top.append({"statement": dims.get("db.sql_tokenized.statement", ""), "aas": avg})
    top.sort(key=lambda x: x["aas"], reverse=True)
    return {"top_sql": top[:10]}
