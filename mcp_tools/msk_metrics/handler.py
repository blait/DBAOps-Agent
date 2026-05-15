"""msk_metrics Lambda — CloudWatch MSK + JMX-via-Prometheus 통합.

입력: {"cluster_arn", "metric", "start", "end"}
출력: {"series": [...]}
"""

from __future__ import annotations

import json
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cw = boto3.client("cloudwatch")


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    cluster_name = body["cluster_arn"].split("/")[-2]
    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Kafka",
                        "MetricName": body["metric"],
                        "Dimensions": [{"Name": "Cluster Name", "Value": cluster_name}],
                    },
                    "Period": 60,
                    "Stat": body.get("stat", "Average"),
                },
                "ReturnData": True,
            }
        ],
        StartTime=body["start"],
        EndTime=body["end"],
    )
    pts = resp.get("MetricDataResults", [{}])[0]
    series = [
        {"ts": ts.isoformat(), "value": float(v)}
        for ts, v in zip(pts.get("Timestamps", []), pts.get("Values", []))
    ]
    return {"series": series}
