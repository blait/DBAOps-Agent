"""baseline workload — PG 50 TPS / MySQL 30 QPS / Kafka 100 msg/s 모사."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def run(duration_sec: int) -> int:
    end = time.time() + duration_sec
    while time.time() < end:
        # TODO: Phase 2에서 실제 PG/MySQL/Kafka 클라이언트 호출
        time.sleep(1.0)
    logger.info("baseline finished")
    return 0
