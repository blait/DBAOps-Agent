# DBAOps-Agent

LangGraph + AWS Bedrock AgentCore 기반 DB·인프라 메트릭 분석 PoC 에이전트.

자연어 요청과 시간 범위만 던지면 OS / DB-perf / Log 세 도메인을 교차 상관해 **추세 + 이상 지점 + 가설 + 다음 확인 항목**을 구조화된 리포트로 돌려준다.

자세한 설계는 [`docs/PLAN.md`](docs/PLAN.md) 참고.

## 구조

```
agent/        LangGraph 애플리케이션 (AgentCore Runtime 컨테이너)
mcp_tools/    MCP 도구 6종 (Lambda)
ui/streamlit/ Streamlit 웹 UI
generators/   데이터·로그 생성기 (ECS Fargate Spot)
infra/        Terraform (서울 단일 리전)
schemas/      JSON Schema (요청·리포트·MCP I/O)
scripts/      운영 스크립트 (bootstrap, gateway 등록 등)
docs/         설계 문서
```

## 빠른 시작 (PoC)

```bash
# 1. AgentCore 서울 GA 가드
bash scripts/verify_agentcore_seoul.sh

# 2. TF 백엔드 부트스트랩
make bootstrap

# 3. 인프라 배포
make plan
make apply

# 4. 에이전트 컨테이너 빌드 + 등록
make deploy-agent

# 5. 데모 워크로드 시작
make demo-up
```

자세한 단계는 `docs/PLAN.md` Phase 1~5 참고.

## 리전 / 모델

- 리전: `ap-northeast-2` (서울)
- LLM: Bedrock Opus 4.7 (`claude-opus-4-7`) — 단일 모델

## 라이선스

내부 PoC. 외부 공개 전 별도 검토 필요.
