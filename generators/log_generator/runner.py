"""log generator entrypoint — pg / mysql / kafka 라인을 S3 + CW Logs 로 송출."""

from __future__ import annotations

import logging
import os
import time

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("log_generator")


def main() -> int:
    source = os.environ.get("SOURCE", "postgres")
    mode = os.environ.get("MODE", "baseline")  # baseline | burst
    duration = int(os.environ.get("DURATION_SEC", "120"))
    logger.info("source=%s mode=%s duration=%ds", source, mode, duration)
    end = time.time() + duration
    while time.time() < end:
        # TODO: Phase 2에서 source/mode 별 라인 생성 + S3/CWLogs 송출
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
