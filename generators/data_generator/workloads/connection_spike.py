"""connection_spike — PG 10초간 200 short connection burst."""

from __future__ import annotations

import logging
import threading
import time

import psycopg

from .._secrets import pg_dsn

logger = logging.getLogger(__name__)


# 모듈 전역 카운터 (스레드 간 공유) — 한 번 burst 실행 단위로 reset
_lock = threading.Lock()
_succ = 0
_fail = 0
_first_err: Exception | None = None


def _short_conn_burst(dsn: dict, hold_sec: float) -> None:
    """connection 1개를 만들고 hold_sec 동안 잡고 있다 닫는다.

    너무 짧게 닫으면 RDS DatabaseConnections (1min 집계) 에 안 잡힌다.
    """
    global _succ, _fail, _first_err
    try:
        conn = psycopg.connect(**dsn, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        time.sleep(hold_sec)  # 동시 점유 시간을 늘려 메트릭 노출
        conn.close()
        with _lock:
            _succ += 1
    except Exception as e:  # noqa: BLE001
        with _lock:
            _fail += 1
            if _first_err is None:
                _first_err = e


def run(duration_sec: int, burst_conns: int = 200, burst_window_sec: int = 10,
        hold_sec: float = 3.0) -> int:
    """duration_sec 안에서 burst 1회 실행.

    - burst_conns 만큼 thread 를 띄워 동시에 connection 들이 hold_sec 동안 살아있게 한다.
    - 결과는 success / fail 카운트와 첫 에러 메시지를 INFO 로그로 출력.
    """
    global _succ, _fail, _first_err
    _succ = 0
    _fail = 0
    _first_err = None

    dsn = pg_dsn()
    logger.info("connection_spike: %d threads, hold=%.1fs (target window=%ds)",
                burst_conns, hold_sec, burst_window_sec)

    threads = [
        threading.Thread(target=_short_conn_burst, args=(dsn, hold_sec), daemon=True)
        for _ in range(burst_conns)
    ]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=hold_sec + burst_window_sec + 10)
    elapsed = time.time() - t0

    logger.info("burst done in %.2fs: success=%d fail=%d", elapsed, _succ, _fail)
    if _first_err is not None:
        logger.warning("first connection error: %s", _first_err)

    # duration_sec 남은 만큼 그냥 idle (메트릭 안정화)
    remaining = max(0.0, duration_sec - elapsed)
    if remaining > 0:
        logger.info("idle for %.1fs (post-burst)", remaining)
        time.sleep(remaining)

    logger.info("connection_spike finished")
    return 0
