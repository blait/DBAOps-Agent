"""Bedrock Opus 4.7 LLM 클라이언트."""

from __future__ import annotations

import os
from functools import lru_cache

from langchain_aws import ChatBedrockConverse


@lru_cache(maxsize=1)
def get_llm() -> ChatBedrockConverse:
    region = os.environ.get("BEDROCK_REGION", "ap-northeast-2")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "claude-opus-4-7")
    return ChatBedrockConverse(
        model=model_id,
        region_name=region,
        temperature=0.0,
        max_tokens=4096,
    )
