# DBAOps-Agent (PoC)

LangGraph + AWS Bedrock AgentCore + MCP 기반 DB·인프라 분석 에이전트. 자연어로 "최근 1시간 EC2 CPU peak 보여줘" 라고 물으면 → AI 분석가가 도구를 직접 골라 호출 → 검증 단계로 거짓말·인용 누락을 거른 다음 → 차트 포함 markdown 리포트로 답한다.

이 repo 는 **시연용 PoC** — Aurora PG / RDS MySQL / MSK / Prometheus 와 시나리오 generator 까지 포함된 통합본. 고객 환경 배포는 별도 repo [`DBAOps-Agent-nonTestbed`](https://github.com/blait/DBAOps-Agent-nonTestbed) 사용.

> **처음 배포라면 → [docs/POC_QUICKSTART.md](docs/POC_QUICKSTART.md) 25단계 가이드** 를 따라가세요.
>
> 시스템이 어떻게 동작하는지 → [docs/SERVICE_GUIDE.md](docs/SERVICE_GUIDE.md).

---

## 무엇이 들어있나

```
agent/        LangGraph 파이프라인 (3 도메인 × validation × report) + single 에이전트
ui/streamlit/ Streamlit 4 탭 UI + 시나리오 라이브 모니터
mcp_tools/    10 MCP Lambda (4 우리 PoC + 3 awslabs + 3 community)
generators/   시나리오 부하 generator (data 7종 + log burst 3종)
infra/        Terraform — 14 module (test bed + agent + UI 까지 풀 스택)
scripts/      build / register / demo_up / demo_down 등
docs/         POC_QUICKSTART, SERVICE_GUIDE
```

---

## Quick reference (이미 한 번 띄워본 사람)

```bash
# 0. 사전: AWS 자격증명 + Bedrock Opus 4.7 access
bash scripts/verify_agentcore_seoul.sh

# 1. State backend
make bootstrap

# 2. 1차 apply (인프라 + ECR repo)
make plan
make apply       # mcp_images_pushed=false, streamlit_image_pushed=false

# 3. 이미지 4종 빌드
bash scripts/build_generator_images.sh
bash scripts/build_mcp_images.sh
bash scripts/build_agent_image.sh
bash scripts/build_streamlit_image.sh

# 4. 2차 apply (MCP Lambda)
cd infra/envs/poc
terraform apply -var=mcp_images_pushed=true -var=streamlit_image_pushed=false

# 5. AgentCore 등록
cd ../../..
ENV=poc python scripts/register_gateway_targets.py

# 6. 3차 apply (Streamlit)
cd infra/envs/poc
RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-2 --no-cli-pager --output json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(r['agentRuntimeArn'] for r in d['agentRuntimes'] if r['agentRuntimeName']=='dbaops_poc'))")
terraform apply \
  -var=mcp_images_pushed=true -var=streamlit_image_pushed=true \
  -var="agentcore_runtime_arn=${RUNTIME_ARN}"

# 7. 시나리오 generator 활성
bash scripts/demo_up.sh                 # 모든 EventBridge schedule ENABLED
# 또는 ad-hoc 1 회
bash scripts/demo_up.sh data-slow-query

# 8. 접속
terraform output streamlit_url
```

처음 배포라면 위 흐름의 각 단계가 무엇이고 어떻게 검증·트러블슈팅할지 **[POC_QUICKSTART.md](docs/POC_QUICKSTART.md)** 에 상세 25단계로.

---

## Make targets

| target | 동작 |
|---|---|
| `make bootstrap`   | TF state S3 + DynamoDB lock 부트스트랩 |
| `make verify`      | AgentCore 서울 GA 가드 |
| `make plan`        | terraform plan (envs/poc) |
| `make apply`       | terraform apply (envs/poc) |
| `make deploy-agent`| agent 컨테이너 빌드 + Runtime 갱신 |
| `make demo-up`     | 시나리오 generator schedule 활성 |
| `make demo-down`   | 시나리오 generator schedule 비활성 |
| `make destroy`     | 전체 인프라 제거 |

---

## 데모 종료 시

비용 절감 (자원 보존):
```bash
bash scripts/demo_down.sh                          # cron 끄기
bash scripts/demo_down.sh --stop-tasks             # + 실행 중 task 도 stop
bash scripts/demo_down.sh --streamlit-down         # + Streamlit task 도 0
```

완전 destroy 는 [POC_QUICKSTART.md §24](docs/POC_QUICKSTART.md) 참조.

---

## 리전 / 모델

- 리전: `ap-northeast-2` (서울)
- LLM: Bedrock Opus 4.7 (`global.anthropic.claude-opus-4-7`) — cross-region inference profile

---

## 라이선스

내부 PoC. 외부 공개 전 별도 검토 필요.
