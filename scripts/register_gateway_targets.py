"""Register MCP Lambda targets onto an AgentCore Gateway (idempotent).

Phase 1: Gateway / Cognito 가 Terraform 으로 만들어진 후 outputs 를 읽어 처리.
지금은 자리만 잡아둔다.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("register_gateway_targets")

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "mcp_tools"


def load_tool_specs() -> list[dict]:
    specs = []
    for p in sorted(TOOLS_DIR.glob("*/tool_io.json")):
        specs.append(json.loads(p.read_text()))
    return specs


def main() -> int:
    gateway_id = os.environ.get("GATEWAY_ID")
    if not gateway_id:
        logger.warning("GATEWAY_ID env not set — skipping (Phase 1 placeholder).")
        for s in load_tool_specs():
            logger.info("would register: %s", s["name"])
        return 0
    # TODO: boto3 bedrock-agentcore-control CreateGatewayTarget 멱등 호출
    return 0


if __name__ == "__main__":
    sys.exit(main())
