#!/usr/bin/env bash
# 5개 MCP Lambda 컨테이너 이미지 빌드/push (linux/arm64).
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TAG="${TAG:-latest}"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR}"

ROOT="$(dirname "$0")/.."
cd "${ROOT}/mcp_tools"

TOOLS=(cloudwatch_metrics rds_pi sql_readonly msk_metrics s3_log_fetch)
NAMES=(cloudwatch-metrics rds-pi sql-readonly msk-metrics s3-log-fetch)

for i in "${!TOOLS[@]}"; do
  tool="${TOOLS[$i]}"
  name="${NAMES[$i]}"
  echo "==> ${name} (from ${tool}/Dockerfile)"
  docker buildx build --platform linux/arm64 \
    -f "${tool}/Dockerfile" \
    -t "${ECR}/dbaops-mcp-${name}:${TAG}" \
    --provenance false \
    --push "${tool}"
done

echo "pushed 5 mcp images"
