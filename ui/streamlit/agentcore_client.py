"""AgentCore Runtime invoke wrapper for Streamlit UI."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

REGION = os.environ.get("BEDROCK_REGION", "ap-northeast-2")
RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
SERVICE_NAME = os.environ.get("AGENTCORE_SERVICE_NAME", "bedrock-agentcore")


_client = None


def _get_client():
    """boto3 클라이언트를 한 번만 만든다.

    'bedrock-agent-runtime' 은 옛 Bedrock Agents 서비스라 invoke_agent_runtime 이 없다 —
    절대 fallback 으로 쓰지 말 것.
    """
    global _client
    if _client is None:
        _client = boto3.client(SERVICE_NAME, region_name=REGION)
        if not hasattr(_client, "invoke_agent_runtime"):
            raise RuntimeError(
                f"boto3 service '{SERVICE_NAME}' has no invoke_agent_runtime — "
                f"upgrade boto3 (current={boto3.__version__})"
            )
    return _client


def invoke(request: dict[str, Any]) -> dict[str, Any]:
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
