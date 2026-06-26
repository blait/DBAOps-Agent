"""DBAOps Slack 봇 — Socket Mode.

대화형 흐름 (Claude Code 스타일):
  1. @봇 멘션 + 질문 → 곧바로 유연한 단일 에이전트(single)가 응답.
     모드 선택 버튼 없음 — 에이전트가 질문 성격(잡담/조회/원인분석)을 스스로 판단.
  2. 같은 스레드 안에서는 멘션 없이 이어 말해도 같은 세션으로 대화가 계속됨
     (session_id = thread_ts → agent 가 이전 맥락 기억). 되묻기→답변→이어가기 자연스럽게.

Socket Mode 이므로 공개 엔드포인트 불필요 — 봇이 Slack 으로 outbound WebSocket 만 건다.
프라이빗 EC2 + egress 만으로 동작. agent 는 같은 박스의 AGENT_HTTP_URL 로 호출.

env:
  SLACK_BOT_TOKEN   xoxb-...   (chat:write, app_mentions:read, channels:history)
  SLACK_APP_TOKEN   xapp-...   (Socket Mode, connections:write)
  AGENT_HTTP_URL    http://agent:8080/invocations
  STREAMLIT_URL     (선택) 차트 전체 보기 링크
"""

from __future__ import annotations

import logging
import os
import re
import threading
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

# 활성 스레드 집합 — 멘션으로 한 번 대화를 시작한 스레드는 멘션 없이 이어 말해도 응답.
# 봇 재시작 시 초기화(실용상 충분).
_ACTIVE_THREADS: set[str] = set()


def _session_id(thread_ts: str) -> str:
    """Slack 스레드 타임스탬프 → 안정적인 session_id. 같은 스레드 = 같은 대화."""
    return "slk-" + thread_ts.replace(".", "")


def _strip_mention(text: str) -> str:
    """'<@U123> 질문내용' → '질문내용'."""
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def _window() -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=DEFAULT_WINDOW_HOURS)
    return {"start": start.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds")}


@app.event("app_mention")
def on_mention(event, client):
    question = _strip_mention(event.get("text", ""))
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel = event["channel"]
    if not question:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="안녕하세요 — DB·인프라 관련해서 뭐든 물어보세요. "
                 "예: `@DBAOps 최근 1시간 Aurora CPU 어때?`")
        return
    _start_chat(client, channel, thread_ts, question)


def _run_chat(client, channel: str, thread_ts: str, status_ts: str,
              question: str) -> None:
    """백그라운드 스레드에서 agent(single) 호출 + Slack 업데이트 (3초 ack 제한 회피)."""
    request = {
        "mode": "single",
        "free_text": question,
        "time_range": _window(),
        "session_id": _session_id(thread_ts),   # 스레드 = 세션 → agent 가 이전 맥락 기억
    }
    renderer = SlackThreadRenderer(
        client, channel, thread_ts, status_ts, streamlit_url=STREAMLIT_URL or None,
    )
    try:
        for ev in agentcore_client.invoke_stream(request):
            renderer.handle(ev)
    except Exception as e:  # noqa: BLE001
        logger.exception("chat failed")
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f"❌ 실행 오류: {e!r}")
        except Exception:  # noqa: BLE001
            pass


def _start_chat(client, channel: str, thread_ts: str, question: str) -> None:
    """진행상황 메시지 생성 + 백그라운드 대화 시작. 스레드를 활성으로 표시."""
    _ACTIVE_THREADS.add(thread_ts)
    status = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text="⏳ 확인 중…",
    )
    threading.Thread(
        target=_run_chat,
        args=(client, channel, thread_ts, status["ts"], question),
        daemon=True,
    ).start()


@app.event("message")
def on_thread_message(event, client):
    """활성 스레드 안에서 멘션 없이 이어 말하면 같은 세션으로 대화를 계속한다.

    - 봇/시스템 메시지, 멘션 포함 메시지(app_mention 가 처리), 스레드 밖 메시지는 무시.
    - 멘션으로 시작한 적 없는 스레드는 무시(아무 채널 잡담에 끼어들지 않음).
    """
    if event.get("bot_id") or event.get("subtype"):
        return
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts not in _ACTIVE_THREADS:
        return
    text = event.get("text", "")
    if "<@" in text:                        # 멘션 포함 → app_mention 핸들러가 처리
        return
    question = text.strip()
    if not question:
        return
    _start_chat(client, event["channel"], thread_ts, question)


def main() -> None:
    if not agentcore_client.AGENT_HTTP_URL and not agentcore_client.RUNTIME_ARN:
        logger.warning("AGENT_HTTP_URL/AGENTCORE_RUNTIME_ARN 둘 다 미설정 — 호출 실패할 것")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("DBAOps Slack bot starting (Socket Mode)…")
    handler.start()


if __name__ == "__main__":
    main()
