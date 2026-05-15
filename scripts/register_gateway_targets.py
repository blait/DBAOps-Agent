"""Bedrock AgentCore Gateway / Runtime / Targets 멱등 등록.

전제: terraform apply 가 끝났고 다음 환경변수가 채워져 있다.
  - REGION (default ap-northeast-2)
  - ENV (default poc)

수행 단계:
  1. Terraform output 에서 cognito_user_pool_id, app_client_id, gateway_role_arn,
     runtime_role_arn, ecr_repository_url, prometheus_query_lambda_arn 을 읽는다.
  2. Cognito user pool domain 이 없으면 생성 (JWT discoveryUrl 발급용).
  3. Gateway 멱등 생성 (이름 충돌 시 갱신).
  4. mcp_tools/<tool>/tool_io.json 을 inline tool_schema 로 변환해 Lambda target 등록.
  5. Agent runtime 멱등 생성/갱신 (ECR 이미지가 push 되어 있어야 한다).

호출:
  python scripts/register_gateway_targets.py [--skip-runtime]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("register_gateway_targets")

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = ROOT / "infra" / "envs" / os.environ.get("ENV", "poc")
TOOLS_DIR = ROOT / "mcp_tools"
REGION = os.environ.get("REGION", "ap-northeast-2")
GATEWAY_NAME = f"dbaops-{os.environ.get('ENV', 'poc')}"
RUNTIME_NAME = f"dbaops_{os.environ.get('ENV', 'poc')}"
COGNITO_DOMAIN_PREFIX = f"dbaops-{os.environ.get('ENV', 'poc')}-{REGION}"


def tf_output() -> dict[str, Any]:
    res = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=TF_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(res.stdout)
    return {k: v["value"] for k, v in raw.items()}


def ensure_cognito_domain(user_pool_id: str) -> str:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pool = cognito.describe_user_pool(UserPoolId=user_pool_id)["UserPool"]
    if pool.get("Domain"):
        domain = pool["Domain"]
        logger.info("cognito domain exists: %s", domain)
        return domain
    domain = COGNITO_DOMAIN_PREFIX
    cognito.create_user_pool_domain(Domain=domain, UserPoolId=user_pool_id)
    logger.info("created cognito domain %s", domain)
    return domain


def discovery_url(user_pool_id: str) -> str:
    return f"https://cognito-idp.{REGION}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"


def find_gateway(client, name: str) -> dict | None:
    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw.get("name") == name:
                return gw
    return None


def wait_gateway_ready(client, gw_id: str, max_wait_sec: int = 120) -> None:
    import time

    elapsed = 0
    while elapsed < max_wait_sec:
        st = client.get_gateway(gatewayIdentifier=gw_id).get("status")
        if st == "READY":
            return
        logger.info("gateway %s status=%s — waiting", gw_id, st)
        time.sleep(5)
        elapsed += 5
    raise TimeoutError(f"gateway {gw_id} not READY in {max_wait_sec}s")


def upsert_gateway(client, role_arn: str, user_pool_id: str, app_client_id: str) -> dict:
    existing = find_gateway(client, GATEWAY_NAME)
    auth_cfg = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url(user_pool_id),
            "allowedClients": [app_client_id],
        }
    }
    proto_cfg = {
        "mcp": {
            "supportedVersions": ["2025-03-26"],
            "instructions": "DBAOps MCP gateway — 6 tools.",
            "searchType": "SEMANTIC",
        }
    }
    if existing:
        gw_id = existing["gatewayId"]
        # 이미 같은 role/auth 라면 update 호출하지 않음 (UPDATING 상태 회피)
        try:
            full = client.get_gateway(gatewayIdentifier=gw_id)
            same_role = full.get("roleArn") == role_arn
            same_clients = (
                full.get("authorizerConfiguration", {})
                .get("customJWTAuthorizer", {})
                .get("allowedClients")
                == [app_client_id]
            )
            if same_role and same_clients:
                logger.info("gateway %s already in desired state — skipping update", GATEWAY_NAME)
                wait_gateway_ready(client, gw_id)
                return full
        except ClientError as e:  # noqa: BLE001
            logger.warning("get_gateway failed: %s — proceeding with update", e)
        logger.info("updating gateway %s (%s)", GATEWAY_NAME, gw_id)
        client.update_gateway(
            gatewayIdentifier=gw_id,
            name=GATEWAY_NAME,
            description="DBAOps PoC gateway",
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration=proto_cfg,
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=auth_cfg,
        )
        wait_gateway_ready(client, gw_id)
        return client.get_gateway(gatewayIdentifier=gw_id)
    logger.info("creating gateway %s", GATEWAY_NAME)
    created = client.create_gateway(
        name=GATEWAY_NAME,
        description="DBAOps PoC gateway",
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration=proto_cfg,
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration=auth_cfg,
    )
    wait_gateway_ready(client, created["gatewayId"])
    return created


def list_targets(client, gateway_id: str) -> list[dict]:
    paginator = client.get_paginator("list_gateway_targets")
    out = []
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        out.extend(page.get("items", []))
    return out


_ALLOWED_SCHEMA_KEYS = {"type", "properties", "required", "items", "description"}


def _sanitize_schema(node: Any) -> Any:
    """AgentCore inputSchema 가 허용하는 키만 남긴다 (default/enum/format/minLength 등 제거)."""
    if isinstance(node, dict):
        out: dict = {}
        for k, v in node.items():
            if k in _ALLOWED_SCHEMA_KEYS:
                out[k] = _sanitize_schema(v)
        return out
    if isinstance(node, list):
        return [_sanitize_schema(x) for x in node]
    return node


def schema_to_tool_def(spec: dict) -> dict:
    """tool_io.json 을 AgentCore inlinePayload tool 정의로 변환."""
    return {
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "inputSchema": _sanitize_schema(spec.get("input_schema") or {"type": "object"}),
        "outputSchema": _sanitize_schema(spec.get("output_schema") or {"type": "object"}),
    }


def upsert_target(
    client,
    gateway_id: str,
    target_name: str,
    lambda_arn: str,
    tools: list[dict],
):
    cfg = {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {"inlinePayload": tools},
            }
        }
    }
    existing = next(
        (t for t in list_targets(client, gateway_id) if t.get("name") == target_name),
        None,
    )
    if existing:
        tid = existing["targetId"]
        logger.info("updating target %s (%s)", target_name, tid)
        return client.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=tid,
            name=target_name,
            targetConfiguration=cfg,
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
    logger.info("creating target %s", target_name)
    return client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        targetConfiguration=cfg,
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )


def upsert_runtime(client, role_arn: str, ecr_uri: str, gateway_endpoint: str) -> dict | None:
    image_uri = f"{ecr_uri}:latest"
    cfg = {"containerConfiguration": {"containerUri": image_uri}}

    paginator = client.get_paginator("list_agent_runtimes")
    existing = None
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == RUNTIME_NAME:
                existing = rt
                break
        if existing:
            break

    env_vars = {
        "BEDROCK_REGION": REGION,
        "BEDROCK_MODEL_ID": "claude-opus-4-7",
        "GATEWAY_ENDPOINT": gateway_endpoint,
        "TOOL_BUDGET": "32",
    }

    if existing:
        rid = existing["agentRuntimeId"]
        logger.info("updating agent runtime %s", rid)
        return client.update_agent_runtime(
            agentRuntimeId=rid,
            description="DBAOps PoC agent runtime",
            roleArn=role_arn,
            agentRuntimeArtifact=cfg,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env_vars,
        )
    logger.info("creating agent runtime %s", RUNTIME_NAME)
    return client.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="DBAOps PoC agent runtime",
        roleArn=role_arn,
        agentRuntimeArtifact=cfg,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=env_vars,
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-runtime", action="store_true", help="agent runtime 등록은 건너뜀 (이미지 push 전)")
    args = p.parse_args(argv)

    outputs = tf_output()
    user_pool_id = outputs["cognito_user_pool_id"]
    app_client_id = outputs["cognito_app_client_id"]
    gateway_role = outputs["agentcore_gateway_role_arn"]
    runtime_role = outputs["agentcore_runtime_role_arn"]
    ecr_uri = outputs["ecr_repository_url"]
    prom_lambda = outputs["prometheus_query_lambda_arn"]

    ensure_cognito_domain(user_pool_id)

    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    gw = upsert_gateway(ac, gateway_role, user_pool_id, app_client_id)
    gw_id = gw.get("gatewayId") or gw["gatewayIdentifier"]
    gw_url = gw.get("gatewayUrl") or gw.get("mcpEndpoint") or ""
    logger.info("gateway id=%s url=%s", gw_id, gw_url)

    # Phase 1 등록 도구: prometheus_query 만 (나머지 Lambda 는 Phase 2)
    spec = json.loads((TOOLS_DIR / "prometheus_query" / "tool_io.json").read_text())
    upsert_target(
        ac,
        gw_id,
        target_name="prometheus-query",
        lambda_arn=prom_lambda,
        tools=[schema_to_tool_def(spec)],
    )

    if args.skip_runtime:
        logger.info("--skip-runtime 지정 — agent runtime 등록 생략")
    else:
        try:
            upsert_runtime(ac, runtime_role, ecr_uri, gw_url)
        except ClientError as e:
            logger.error("agent runtime upsert failed: %s", e)
            return 2

    print(json.dumps({"gateway_id": gw_id, "gateway_url": gw_url}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
