"""AgentCore Runtime invoke wrapper for Streamlit UI."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterator

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

REGION = os.environ.get("BEDROCK_REGION", "ap-northeast-2")
RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
SERVICE_NAME = os.environ.get("AGENTCORE_SERVICE_NAME", "bedrock-agentcore")
# swarm streaming 은 LLM 호출이 길게 이어져 read 사이 60s+ 공백이 흔하다 — 충분히 길게.
READ_TIMEOUT = int(os.environ.get("AGENTCORE_READ_TIMEOUT", "900"))
CONNECT_TIMEOUT = int(os.environ.get("AGENTCORE_CONNECT_TIMEOUT", "10"))


_client = None


def _get_client():
    global _client
    if _client is None:
        cfg = Config(
            read_timeout=READ_TIMEOUT,
            connect_timeout=CONNECT_TIMEOUT,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        _client = boto3.client(SERVICE_NAME, region_name=REGION, config=cfg)
        if not hasattr(_client, "invoke_agent_runtime"):
            raise RuntimeError(
                f"boto3 service '{SERVICE_NAME}' has no invoke_agent_runtime — "
                f"upgrade boto3 (current={boto3.__version__})"
            )
    return _client


def invoke(request: dict[str, Any]) -> dict[str, Any]:
    """단발 호출 — 응답 전체를 한 번에 받음 (fast 모드용)."""
    if not RUNTIME_ARN:
        return {"error": "AGENTCORE_RUNTIME_ARN env not set"}

    payload = json.dumps({"request": request}).encode()
    try:
        client = _get_client()
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            payload=payload,
            contentType="application/json",
        )
        body = resp.get("response") or resp.get("body")
        if hasattr(body, "read"):
            body = body.read()
        return json.loads(body) if body else {}
    except Exception as e:  # noqa: BLE001
        logger.exception("invoke_agent_runtime failed")
        return {"error": f"AgentCore invoke failed: {e!r}"}


def invoke_stream(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """NDJSON streaming 호출 — 한 줄당 한 이벤트 yield (swarm 모드용).

    Runtime 컨테이너가 application/x-ndjson 으로 chunked 응답하면 boto3 StreamingBody 가
    그대로 chunk 를 노출하므로 줄 단위로 파싱한다.
    """
    if not RUNTIME_ARN:
        yield {"type": "error", "error": "AGENTCORE_RUNTIME_ARN env not set"}
        return

    body_bytes = json.dumps({"request": {**request, "stream": True}}).encode()
    try:
        client = _get_client()
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            payload=body_bytes,
            contentType="application/json",
            accept="application/x-ndjson",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("invoke_agent_runtime (stream) failed")
        yield {"type": "error", "error": f"AgentCore invoke failed: {e!r}"}
        return

    body = resp.get("response") or resp.get("body")
    if body is None:
        yield {"type": "error", "error": "empty response"}
        return

    buf = b""
    try:
        for chunk in body.iter_chunks() if hasattr(body, "iter_chunks") else iter(lambda: body.read(4096), b""):
            if not chunk:
                continue
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1:]
                if not line.strip():
                    continue
                try:
                    yield json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    logger.warning("ndjson parse failed: %s | %s", e, line[:200])
        # tail (no trailing newline)
        tail = buf.strip()
        if tail:
            try:
                yield json.loads(tail.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                pass
    except Exception as e:  # noqa: BLE001
        logger.exception("stream read failed")
        yield {"type": "error", "error": f"stream read failed: {e!r}"}
