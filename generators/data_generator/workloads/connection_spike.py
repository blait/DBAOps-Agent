"""connection_spike — PG 10초간 200 short connection burst."""

from __future__ import annotations

import logging
import threading
import time

import psycopg

from .._secrets import pg_dsn

logger = logging.getLogger(__name__)


def _short_conn_burst(dsn: dict) -> None:
    try:
        conn = psycopg.connect(**dsn, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        time.sleep(0.05)
        conn.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("short conn err: %s", e)


def run(duration_sec: int, burst_conns: int = 200, burst_window_sec: int = 10) -> int:
    dsn = pg_dsn()
    end = time.time() + duration_sec
    while time.time() < end:
        threads = [
            threading.Thread(target=_short_conn_burst, args=(dsn,), daemon=True)
            for _ in range(burst_conns)
        ]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=burst_window_sec + 5)
        elapsed = time.time() - t0
        logger.info("burst %d conns in %.2fs", burst_conns, elapsed)
        # 대기 후 다음 burst (전체 duration 안에 1회만 기본)
        time.sleep(max(0.0, burst_window_sec - elapsed))
        break
    logger.info("connection_spike finished")
    return 0
