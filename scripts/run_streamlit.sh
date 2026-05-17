#!/usr/bin/env bash
# Streamlit UI 띄우기 — 환경변수는 terraform output 에서 자동 추출.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
PORT="${PORT:-8502}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="${ROOT}/infra/envs/poc"
UI_DIR="${ROOT}/ui/streamlit"

# Runtime ARN
RUNTIME_NAME="${RUNTIME_NAME:-dbaops_poc}"
RUNTIME_ARN="$(aws bedrock-agentcore-control list-agent-runtimes --region "${REGION}" \
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeArn | [0]" --output text)"

# Terraform outputs
TF_OUT="$(cd "${TF_DIR}" && terraform output -json)"
SUBNETS="$(echo "${TF_OUT}" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['private_subnet_ids']['value']))")"
SG="$(aws ec2 describe-security-groups --region "${REGION}" \
    --filters "Name=group-name,Values=dbaops-poc-gen-*" \
    --query 'SecurityGroups[0].GroupId' --output text)"

cd "${UI_DIR}"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Streamlit launching"
echo "    runtime: ${RUNTIME_ARN}"
echo "    subnets: ${SUBNETS}"
echo "    sg:      ${SG}"
echo "    http://localhost:${PORT}"

AGENTCORE_RUNTIME_ARN="${RUNTIME_ARN}" \
BEDROCK_REGION="${REGION}" \
AWS_REGION="${REGION}" \
ECS_CLUSTER="dbaops-poc" \
ECS_SUBNETS="${SUBNETS}" \
ECS_SECURITY_GROUPS="${SG}" \
streamlit run app.py --server.headless true --server.port "${PORT}"
