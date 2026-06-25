"""DBAOps Slack 봇 — Socket Mode.

흐름:
  1. 워크스페이스에서 @봇 멘션 → 질문 텍스트 파싱
  2. Block Kit 버튼으로 분석 모드 선택 (OS·인프라 / DB 성능 / 로그 / 단일 RCA)
  3. 버튼 클릭 → 해당 mode 로 agent 호출 (invoke_stream) → 스레드에 진행/리포트 게시

Socket Mode 이므로 공개 엔드포인트 불필요 — 봇이 Slack 으로 outbound WebSocket 만 건다.
프라이빗 EC2 + egress 만으로 동작. agent 는 같은 박스의 AGENT_HTTP_URL 로 호출.

env:
  SLACK_BOT_TOKEN   xoxb-...   (chat:write, app_mentions:read)
  SLACK_APP_TOKEN   xapp-...   (Socket Mode, connections:write)
  AGENT_HTTP_URL    http://agent:8080/invocations
  STREAMLIT_URL     (선택) 차트 전체 보기 링크
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import agentcore_client
from render import SlackThreadRenderer

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("dbaops-slack")

STREAMLIT_URL = os.environ.get("STREAMLIT_URL", "")
DEFAULT_WINDOW_HOURS = int(os.environ.get("SLACK_DEFAULT_WINDOW_HOURS", "1"))

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# 분석 모드 정의 — value 에 mode/domain 인코딩
_MODES = [
    {"label": "🖥️ OS·인프라", "mode": "pipeline", "domain": "os_metric"},
    {"label": "🗄️ DB 성능",   "mode": "pipeline", "domain": "db_metric"},
    {"label": "📜 로그",       "mode": "pipeline", "domain": "log"},
    {"label": "🧠 단일 RCA",   "mode": "single",   "domain": None},
]


def _strip_mention(text: str) -> str:
    """'<@U123> 질문내용' → '질문내용'."""
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def _window() -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=DEFAULT_WINDOW_HOURS)
    return {"start": start.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds")}


def _mode_buttons(question: str) -> list:
    """질문을 각 버튼 value 에 실어 도메인 선택 Block 구성."""
    elements = []
    for i, m in enumerate(_MODES):
        payload = json.dumps({"mode": m["mode"], "domain": m["domain"], "q": question})
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": m["label"]},
            "value": payload[:1900],     # Slack value 한도 2000
            "action_id": f"dbaops_mode_{i}",
        })
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*분석 요청:* {question}\n어떤 분석으로 실행할까요?"}},
        {"type": "actions", "elements": elements},
    ]


@app.event("app_mention")
def on_mention(event, say):
    question = _strip_mention(event.get("text", ""))
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not question:
        say(text="질문을 함께 적어주세요. 예: `@DBAOps 최근 1시간 CPU peak 분석`",
            thread_ts=thread_ts)
        return
    say(blocks=_mode_buttons(question), text="분석 모드를 선택하세요.", thread_ts=thread_ts)


def _run_analysis(client, channel: str, thread_ts: str, status_ts: str,
                  mode: str, domain: str | None, question: str) -> None:
    """백그라운드 스레드에서 agent 호출 + Slack 업데이트 (Slack 3초 ack 제한 회피)."""
    request = {
        "mode": mode,
        "free_text": question,
        "time_range": _window(),
        "session_id": str(uuid.uuid4())[:8],
    }
    if mode == "pipeline":
        request["domain"] = domain

    renderer = SlackThreadRenderer(
        client, channel, thread_ts, status_ts, streamlit_url=STREAMLIT_URL or None,
    )
    try:
        for ev in agentcore_client.invoke_stream(request):
            renderer.handle(ev)
    except Exception as e:  # noqa: BLE001
        logger.exception("analysis failed")
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f"❌ 실행 오류: {e!r}")
        except Exception:  # noqa: BLE001
            pass


@app.action(re.compile(r"dbaops_mode_\d+"))
def on_mode_select(ack, body, client):
    ack()
    action = body["actions"][0]
    payload = json.loads(action["value"])
    mode = payload["mode"]
    domain = payload.get("domain")
    question = payload.get("q", "")

    channel = body["channel"]["id"]
    # 버튼이 달린 메시지의 스레드. container 의 message_ts 가 봇 메시지 ts.
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]

    label = next((m["label"] for m in _MODES
                  if m["mode"] == mode and m["domain"] == domain), mode)

    # 진행상황 메시지 1개 생성 → 이후 chat_update 로 갱신
    status = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"⏳ `{label}` 분석을 준비합니다…",
    )
    status_ts = status["ts"]

    threading.Thread(
        target=_run_analysis,
        args=(client, channel, thread_ts, status_ts, mode, domain, question),
        daemon=True,
    ).start()


def main() -> None:
    if not agentcore_client.AGENT_HTTP_URL and not agentcore_client.RUNTIME_ARN:
        logger.warning("AGENT_HTTP_URL/AGENTCORE_RUNTIME_ARN 둘 다 미설정 — 호출 실패할 것")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("DBAOps Slack bot starting (Socket Mode)…")
    handler.start()


if __name__ == "__main__":
    main()
