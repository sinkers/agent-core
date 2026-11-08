"""Amazon Bedrock Converse client with prompt caching."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .store import Usage


@dataclass
class ModelReply:
    content: list[dict]  # Bedrock content blocks: {"text":...} or {"toolUse":...}
    stop_reason: str
    usage: Usage
    latency_ms: int = 0

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content)

    @property
    def tool_uses(self) -> list[dict]:
        return [b["toolUse"] for b in self.content if "toolUse" in b]


class ModelClient(Protocol):
    async def converse(self, *, model_id: str, tier: str, system: list[str], messages: list[dict],
                       tool_config: dict | None, max_tokens: int) -> ModelReply: ...


@dataclass
class BedrockClient:
    region: str
    prompt_caching: bool = True
    # A connection left idle can die silently on the way (a sandbox's egress proxy, NAT): reused, the call then
    # waits out the whole read timeout. After this long without a call, the pool is replaced first.
    idle_reconnect_s: float = 45.0
    _client: Any = field(default=None, init=False, repr=False)
    _last_call: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self._new_client()

    def _new_client(self):
        import boto3
        from botocore.config import Config
        return boto3.client(
            "bedrock-runtime", region_name=self.region,
            config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=600,  # a 24k-token reply takes ~4 min
                          connect_timeout=10, tcp_keepalive=True),
        )

    def _fresh(self):
        now = time.monotonic()
        if self._last_call and now - self._last_call > self.idle_reconnect_s:
            old, self._client = self._client, self._new_client()
            try:
                old.close()
            except Exception:
                pass
        self._last_call = now
        return self._client

    def _caches(self, model_id: str) -> bool:
        # Prompt caching is Anthropic-only on Bedrock; other makers reject a cachePoint outright.
        from .config import family_of
        return self.prompt_caching and family_of(model_id) == "anthropic"

    def _system_blocks(self, system: list[str], model_id: str = "anthropic") -> list[dict]:
        blocks: list[dict] = []
        for part in system:
            if part:
                blocks.append({"text": part})
        if self._caches(model_id) and blocks:
            # Everything up to here (system prompt + always-loaded skills) is cached.
            blocks.append({"cachePoint": {"type": "default"}})
        return blocks

    async def converse(self, *, model_id, tier, system, messages, tool_config, max_tokens) -> ModelReply:
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "system": self._system_blocks(system, model_id),
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if tool_config:
            tools = list(tool_config["tools"])
            if self._caches(model_id):
                tools.append({"cachePoint": {"type": "default"}})
            kwargs["toolConfig"] = {"tools": tools}
        resp = await asyncio.to_thread(self._fresh().converse, **kwargs)
        self._last_call = time.monotonic()
        u = resp.get("usage", {})
        usage = Usage(
            model_id=model_id, tier=tier,
            input_tokens=u.get("inputTokens", 0), output_tokens=u.get("outputTokens", 0),
            cache_read_tokens=u.get("cacheReadInputTokens", 0), cache_write_tokens=u.get("cacheWriteInputTokens", 0),
        )
        # Reasoning blocks (Fable, Grok, gpt-oss) are kept for the same model's next step; the agent loop strips
        # them if a different model takes over (agent.py). `text` ignores them.
        content = resp["output"]["message"]["content"]
        return ModelReply(
            content=content, stop_reason=resp.get("stopReason", ""),
            usage=usage, latency_ms=resp.get("metrics", {}).get("latencyMs", 0),
        )
