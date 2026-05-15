"""cloudwatch_metrics Lambda — GetMetricData 정규화.

입력: {"namespace", "metric", "dimensions", "start", "end", "stat", "period"}
출력: {"series": [{"ts", "value"}]}
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

    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": body["namespace"],
                        "MetricName": body["metric"],
                        "Dimensions": [
                            {"Name": k, "Value": v} for k, v in body.get("dimensions", {}).items()
                        ],
                    },
                    "Period": int(body.get("period", 60)),
                    "Stat": body.get("stat", "Average"),
                },
                "ReturnData": True,
            }
        ],
        StartTime=body["start"],
        EndTime=body["end"],
    )
    points = resp.get("MetricDataResults", [{}])[0]
    series = [
        {"ts": ts.isoformat(), "value": float(v)}
        for ts, v in zip(points.get("Timestamps", []), points.get("Values", []))
    ]
    return {"series": series}
