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


def invoke(request: dict[str, Any]) -> dict[str, Any]:
    """Bedrock AgentCore Runtime 호출.

    boto3 service name 은 환경마다 'bedrock-agentcore' 또는 'bedrock-agent-runtime' 로
    바뀔 수 있으니 한쪽이 실패하면 다른 쪽 시도.
    """
    if not RUNTIME_ARN:
        return {"error": "AGENTCORE_RUNTIME_ARN env not set"}

    payload = json.dumps({"request": request}).encode()
    last_err: Exception | None = None
    for service in ("bedrock-agentcore", "bedrock-agent-runtime"):
        try:
            client = boto3.client(service, region_name=REGION)
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
            last_err = e
            logger.warning("invoke via %s failed: %s", service, e)
    return {"error": f"AgentCore invoke failed: {last_err}"}
