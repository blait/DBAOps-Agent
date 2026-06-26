# DBAOps-Agent 올인원 EC2 배포

AgentCore / Gateway / Cognito / Lambda **없이** EC2 한 대에서 전체 시스템을 구동한다.
고객이 받은 권한이 AWS 관리형 `DatabaseAdministrator`(데이터 읽기) 한 장뿐인 환경을 위한 구성.

```
EC2 (instance role: DatabaseAdministrator + bedrock:InvokeModel)
└─ docker compose
   ├─ mcp-router  :9000   MCP 도구 라우터 (AgentCore Gateway 대체)
   ├─ agent       :8080   LangGraph 파이프라인/단일 에이전트
   ├─ streamlit   :8501   웹 UI + 🔌 MCP 연결설정
   └─ slack-bot           Socket Mode (outbound only)
```

---

## 0. 사전 조건 (인프라팀이 준비)

EC2 자체 생성·IAM role 생성은 `DatabaseAdministrator` 로는 불가하므로 **인프라팀이 제공**해야 한다.

| 항목 | 내용 |
|---|---|
| **EC2 인스턴스** | 분석 대상 DB·Prometheus 와 **같은 VPC**(또는 라우팅 가능). 권장 t3.large 이상, 디스크 30GB+ |
| **Instance profile(IAM role)** | 아래 2개 정책을 attach: `DatabaseAdministrator` (관리형) + `bedrock:InvokeModel` 인라인 |
| **Egress** | Bedrock 호출용 (NAT 또는 bedrock-runtime VPC endpoint). 이미지/패키지 pull 용 인터넷 또는 프록시 |
| **인바운드 8501** | Streamlit 접속 경로 (사내망/VPN/보안그룹 제한 권장) |
| **DB 접근 SG** | EC2 → 고객 PG/MySQL(5432/3306), Prometheus(9090) 인바운드 허용 |

instance role 에 추가할 Bedrock 인라인 정책:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "BedrockInvoke",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "*"
  }]
}
```

> `DatabaseAdministrator` 가 이미 커버: rds:* / pi:* / cloudwatch / logs / dynamodb / s3 read.
> 따라서 MCP 도구의 데이터 조회는 추가 권한 없이 동작한다.

---

## 1. EC2 부트스트랩 (docker 설치)

Amazon Linux 2023 기준:

```bash
sudo dnf -y install docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user      # 재로그인 후 sudo 없이 docker 사용
# docker compose v2 플러그인
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -sL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
```

---

## 2. 코드 가져오기 + 설정

```bash
git clone <this-repo> dbaops && cd dbaops/deploy/ec2-allinone

cp .env.example .env
# .env 편집: AWS_REGION, BEDROCK_MODEL_ID, (선택) SLACK_*, STREAMLIT_URL

# 연결설정 초기값 (이후 UI 에서 편집 가능)
docker volume create ec2-allinone_dbaops-data 2>/dev/null || true
# connections.json 은 첫 기동 후 UI 연결설정 탭에서 채워도 됨.
```

---

## 3. 기동

```bash
# Slack 토큰을 .env 에 넣었다면:
docker compose up -d --build

# Slack 없이 먼저 테스트하려면 slack-bot 제외:
docker compose up -d --build mcp-router agent streamlit
```

상태 확인:

```bash
docker compose ps
curl -s localhost:9000/healthz | python3 -m json.tool      # 라우터(컨테이너 내부 네트워크라 호스트에선 안 보일 수 있음)
docker compose logs -f agent
```

---

## 4. 연결 설정 (Streamlit)

1. 브라우저로 `http://<ec2-ip>:8501` 접속
2. **🔌 MCP 연결설정** 탭
3. 사용할 도구 토글 ON + 연결 정보 입력:
   - **Prometheus**: `PROMETHEUS_URL` (예: `http://10.0.0.10:9090`)
   - **PostgreSQL**: Host/Port/DB + (User·Password) 또는 Secrets Manager ARN
   - **MySQL**: 동일
   - CloudWatch / RDS PI / S3 / aws-api: 추가 입력 없음(instance role 권한 사용)
4. **연결 테스트** 로 각 도구 ✅ 확인 → **전체 저장**
5. (선택) 인프라 식별자(aurora writer id 등) 입력 — 비우면 에이전트가 describe 로 직접 탐색

저장하면 라우터가 자동 반영(다음 호출부터). 재시작 불필요.

> 연결정보가 어디서 자동으로 오고 무엇만 사람이 입력해야 하는지(특히 DB 비밀번호),
> 권한이 없을 때의 fallback 은 [`../../docs/CONNECTION_INFO.md`](../../docs/CONNECTION_INFO.md) 참조.

---

## 5. 사용

- **Streamlit**: OS·인프라 / DB 성능 / 로그 / 단일 RCA 탭에서 자연어 질문
- **Slack**: 채널에 봇 초대 후 `@DBAOps 최근 1시간 CPU peak 분석` → 모드 버튼 선택 → 스레드에 결과

---

## 6. Slack 앱 설정 (Socket Mode)

공개 엔드포인트가 필요 없다(봇이 Slack 으로 outbound WebSocket 연결).

1. https://api.slack.com/apps → **Create New App** → From scratch
2. **Socket Mode** → 활성화 → App-Level Token 생성(scope `connections:write`) → `xapp-...` = `SLACK_APP_TOKEN`
3. **OAuth & Permissions** → Bot Token Scopes: `app_mentions:read`, `chat:write` → 워크스페이스 설치 → `xoxb-...` = `SLACK_BOT_TOKEN`
4. **Event Subscriptions** → Enable → Subscribe to bot events: `app_mention`
5. `.env` 에 두 토큰 입력 → `docker compose up -d slack-bot`
6. 대상 채널에서 `/invite @DBAOps`

---

## 7. 갱신 / 종료

```bash
git pull && docker compose up -d --build      # 코드 갱신 후 재빌드
docker compose restart mcp-router             # 라우터만 재시작
docker compose down                           # 종료(볼륨 보존)
docker compose down -v                        # 볼륨까지 삭제(연결설정 초기화)
```

---

## 트러블슈팅

| 증상 | 확인 |
|---|---|
| 채팅이 "호출할 수 없습니다" | streamlit 의 `AGENT_HTTP_URL` env, agent 컨테이너 상태 |
| 도구 연결 ❌ | 연결설정 값, EC2→DB 보안그룹, `docker compose logs mcp-router` |
| LLM 오류(AccessDenied) | instance role 에 `bedrock:InvokeModel` 있는지, 리전/모델ID |
| stdio 도구가 안 뜸 | 라우터 로그에서 spawn 에러(자격증명/네트워크) |
| Slack 무반응 | 봇 토큰, `app_mention` 구독, 채널 초대, `docker compose logs slack-bot` |
| x86 EC2 에서 agent 빌드 실패 | `docker-compose.yml` 의 agent `platform: linux/arm64` 주석 해제 또는 ARM 인스턴스 사용 |
