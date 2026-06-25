"""agent 이벤트 스트림 → Slack 스레드 메시지 변환.

agentcore_client.invoke_stream 이 yield 하는 이벤트(start/stage/message/validation/
report/done/error)를 받아 Slack 스레드를 실시간 업데이트한다.

테스트 단계: 차트는 텍스트로(요약) + 전체는 Streamlit 안내. PNG 첨부는 후속.
"""

from __future__ import annotations

import re

# report markdown 의 ```json-chart ...``` 펜스 제거용
_CHART_FENCE = re.compile(r"```json-chart\s*\n.*?\n```", re.DOTALL)

_STAGE_LABEL = {
    "domain": "🔎 도메인 분석",
    "validation": "🧪 검증",
    "revise": "✍️ 보정",
    "report": "📝 리포트 생성",
}


def strip_charts(markdown: str) -> tuple[str, int]:
    """json-chart 펜스를 제거하고 (정리된 텍스트, 제거된 차트 수) 반환."""
    charts = _CHART_FENCE.findall(markdown or "")
    cleaned = _CHART_FENCE.sub("", markdown or "").strip()
    return cleaned, len(charts)


def truncate(text: str, limit: int = 2900) -> str:
    """Slack 텍스트 블록 한도(3000자) 대비 안전 truncate."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(생략)"


class SlackThreadRenderer:
    """한 분석 요청에 대한 Slack 스레드 업데이트 핸들러.

    say/client 는 slack_bolt 가 주입. channel/thread_ts 로 진행 메시지를 갱신한다.
    """

    def __init__(self, client, channel: str, thread_ts: str, status_ts: str,
                 streamlit_url: str | None = None) -> None:
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.status_ts = status_ts          # 진행상황을 갱신할 메시지 ts
        self.streamlit_url = streamlit_url
        self._last_stage = ""
        self._tool_calls = 0
        self._reported = False          # report 이벤트로 본문을 이미 게시했는지
        self._last_ai_text = ""         # single 모드: 마지막 ai 본문(최종 답변 후보)

    def _update_status(self, text: str) -> None:
        try:
            self.client.chat_update(channel=self.channel, ts=self.status_ts, text=text)
        except Exception:  # noqa: BLE001
            pass

    def _post(self, text: str) -> None:
        try:
            self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts, text=text,
            )
        except Exception:  # noqa: BLE001
            pass

    def handle(self, ev: dict) -> None:
        etype = ev.get("type")

        if etype == "start":
            dom = ev.get("domain") or "single"
            self._update_status(f"🚀 분석 시작 — `{dom}`")

        elif etype == "stage":
            stage = ev.get("stage", "")
            label = _STAGE_LABEL.get(stage, stage)
            status = ev.get("status", "")
            self._update_status(f"{label} {'완료' if status == 'completed' else '진행 중'} "
                                f"(tool calls: {self._tool_calls})")

        elif etype == "message":
            msg = ev.get("message") or {}
            role = msg.get("role")
            if role == "ai":
                tcs = msg.get("tool_calls") or []
                for _ in tcs:
                    self._tool_calls += 1
                if tcs:
                    names = ", ".join(tc.get("name", "?") for tc in tcs)
                    self._update_status(f"🔧 도구 호출: `{names}` (누적 {self._tool_calls})")
                elif (msg.get("text") or "").strip():
                    # tool_call 없는 ai 메시지 = 자연어 답변 → single 모드 최종답변 후보
                    self._last_ai_text = msg["text"]

        elif etype == "validation":
            passed = ev.get("passed")
            issues = ev.get("issues") or []
            if passed:
                self._update_status("✅ 검증 통과")
            else:
                kinds = ", ".join(i.get("kind", "?") for i in issues)
                self._update_status(f"⚠️ 검증 이슈: {kinds} — 보정 중")

        elif etype == "report":
            cleaned, n_charts = strip_charts(ev.get("markdown", ""))
            body = truncate(cleaned)
            if n_charts and self.streamlit_url:
                body += f"\n\n📊 차트 {n_charts}개 — 전체 시각화는 {self.streamlit_url}"
            elif n_charts:
                body += f"\n\n📊 차트 {n_charts}개 (Streamlit UI 에서 시각화)"
            self._post(body or "_(빈 리포트)_")
            self._reported = True

        elif etype == "done":
            # single 모드는 report 이벤트가 없으므로 마지막 ai 본문을 최종 답변으로 게시.
            if not self._reported and self._last_ai_text.strip():
                self._post(truncate(self._last_ai_text))
                self._reported = True
            self._update_status(f"✅ 완료 (메시지 {ev.get('n_messages', '?')} · "
                                f"tool calls {self._tool_calls})")

        elif etype == "abort":
            self._post(f"⏹️ 중단: {ev.get('reason', 'unknown')}")

        elif etype == "error":
            self._post(f"❌ 오류: {ev.get('error', 'unknown')}")
