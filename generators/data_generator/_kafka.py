"""Kafka producer/consumer 헬퍼 — IAM SASL/OAUTHBEARER."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _common_iam_config() -> dict[str, Any]:
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    return {
        "bootstrap.servers": os.environ["MSK_BOOTSTRAP"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "client.id": os.environ.get("KAFKA_CLIENT_ID", "dbaops-generator"),
        # confluent-kafka 가 oauth_cb 콜백을 호출하도록 둠
        "sasl.oauthbearer.method": "default",
        # AWS region 으로 오버라이드
        "sasl.oauthbearer.config": f"region={region}",
    }


def _oauth_cb(_config):
    """AWS MSK IAM SASL token 콜백 — aws_msk_iam_sasl_signer 사용."""
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider  # type: ignore

    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
    return token, expiry_ms / 1000.0


def make_producer():
    from confluent_kafka import Producer

    cfg = _common_iam_config()
    cfg["oauth_cb"] = _oauth_cb
    return Producer(cfg)


def make_consumer(group_id: str, topics: list[str]):
    from confluent_kafka import Consumer

    cfg = _common_iam_config()
    cfg.update(
        {
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "oauth_cb": _oauth_cb,
        }
    )
    c = Consumer(cfg)
    c.subscribe(topics)
    return c
