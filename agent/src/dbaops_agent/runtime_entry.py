"""AgentCore Runtime entrypoint.

AgentCore Runtime 은 컨테이너 내부에서 :8080/invocations 로 들어오는 POST 를 처리하길 기대한다.
(2025년 spec; 환경에 따라 /ping, /invocations 두 endpoint 만 노출하면 된다.)
표준 라이브러리만으로 가벼운 HTTP 서버를 띄워, 외부 의존을 최소화한다.

로컬에서는 `python -m dbaops_agent.runtime_entry --once` 로 단일 invocation smoke 도 가능.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .graph import compile_graph
from .state import AnalysisState

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = compile_graph()
    return _GRAPH


def handler(event: dict, context: Any | None = None) -> dict:
    logger.info("invoke: %s", json.dumps(event)[:500])
    request = event.get("request") or {}
    mode = (request.get("mode") or "fast").lower()

    if mode == "swarm":
        # ReAct/swarm 모드 — 도메인 specialist 3 + 자율 핸드오프
        from .swarm_graph import invoke_swarm
        try:
            result = invoke_swarm(
                request,
                recursion_limit=int(os.environ.get("SWARM_RECURSION_LIMIT", "30")),
            )
            return {"swarm": result, "request": request}
        except Exception as e:  # noqa: BLE001
            logger.exception("swarm invoke failed")
            return {"error": str(e), "request": request}

    # default: fast 모드 — 정해진 LangGraph 흐름
    initial: AnalysisState = {
        "request": request,
        "raw_signals": {},
        "messages": [],
        "tool_budget": int(os.environ.get("TOOL_BUDGET", "32")),
    }
    final = _get_graph().invoke(initial)
    return {"report": final.get("report")}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        logger.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self):  # noqa: N802
        if self.path in ("/ping", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path not in ("/invocations", "/invoke"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            event = json.loads(raw.decode() or "{}")
            result = handler(event)
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.exception("invoke failed")
            err = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err)


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    srv = ThreadingHTTPServer((host, port), _Handler)
    logger.info("serving on %s:%d", host, port)
    srv.serve_forever()


def main(argv: list[str]) -> int:
    if "--once" in argv:
        out = handler({"request": {"free_text": "smoke", "lens": "os"}})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    serve(port=int(os.environ.get("PORT", "8080")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
