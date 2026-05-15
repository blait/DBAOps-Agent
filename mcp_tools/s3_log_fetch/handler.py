"""s3_log_fetch Lambda — gz 로그 byte-range + regex 매칭.

입력: {"bucket", "key", "byte_range": [start, end]?, "regex": str?, "max_lines": 5000}
출력: {"lines": [..], "truncated": bool}
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    bucket = body["bucket"]
    key = body["key"]
    byte_range = body.get("byte_range")
    regex = body.get("regex")
    max_lines = int(body.get("max_lines", 5000))

    kwargs: dict = {"Bucket": bucket, "Key": key}
    if byte_range:
        kwargs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"

    obj = s3.get_object(**kwargs)
    raw = obj["Body"].read()
    if key.endswith(".gz"):
        raw = gzip.decompress(raw)

    pattern = re.compile(regex) if regex else None
    out: list[str] = []
    truncated = False
    for line in io.BytesIO(raw):
        if pattern and not pattern.search(line.decode("utf-8", errors="replace")):
            continue
        out.append(line.decode("utf-8", errors="replace").rstrip())
        if len(out) >= max_lines:
            truncated = True
            break
    return {"lines": out, "truncated": truncated}
