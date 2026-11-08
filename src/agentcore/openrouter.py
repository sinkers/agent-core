"""OpenRouter as a model provider: any tier can use a model from OpenRouter's catalogue instead of Bedrock.

    models:
      openrouter: {api_key_env: OPENROUTER_API_KEY, data_collection: deny}
      tiers:
        fast: {provider: openrouter, model_id: google/gemini-2.5-flash, max_tokens: 1024}

The agent loop speaks Bedrock Converse (content blocks: text, image, document, toolUse, toolResult). This client
translates those messages to OpenRouter's OpenAI-compatible chat API and the reply back, so nothing else changes.
Costs come from OpenRouter's own figure for each call (`usage.cost`), so OpenRouter models need no pricing.yaml
entry; a call that reports no cost is refused by the budget like any unpriced model.

Data: requests go to openrouter.ai and on to the model's provider. `data_collection: deny` (the default) keeps
them away from providers that may train on or store prompts; `zdr: true` restricts to zero-data-retention
endpoints; `providers` sets an order (and, with `allow_fallbacks: false`, a limit). Bedrock keeps data in the
agent's AWS region; OpenRouter does not promise a region.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx

from .bedrock import ModelReply
from .store import Usage

log = logging.getLogger(__name__)
STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "content_filtered"}
MIME = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def _b64(data: Any) -> str:
    return data if isinstance(data, str) else base64.b64encode(data).decode()


def _part(block: dict) -> dict | None:
    """One Converse content block as an OpenAI content part (None: nothing to send)."""
    if "text" in block:
        return {"type": "text", "text": block["text"]}
    if "image" in block:
        img = block["image"]
        mime = MIME.get(img.get("format", "png"), "image/png")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{_b64(img['source']['bytes'])}"}}
    if "document" in block:
        doc = block["document"]
        if doc.get("format") == "pdf":
            return {"type": "file", "file": {"filename": f"{doc.get('name') or 'document'}.pdf",
                                             "file_data": f"data:application/pdf;base64,{_b64(doc['source']['bytes'])}"}}
        data = doc["source"].get("bytes", b"")
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        return {"type": "text", "text": f"[document {doc.get('name') or ''}]\n{text}"}
    if "json" in block:
        return {"type": "text", "text": json.dumps(block["json"])}
    return None  # reasoningContent (another model's), cachePoint


def to_openai(system: list[str], messages: list[dict], cache: bool) -> list[dict]:
    sys_text = "\n\n".join(p for p in system if p)
    out: list[dict] = []
    if sys_text:
        part: dict = {"type": "text", "text": sys_text}
        if cache:
            part["cache_control"] = {"type": "ephemeral"}  # Anthropic models through OpenRouter
        out.append({"role": "system", "content": [part]})
    for msg in messages:
        blocks = msg.get("content") or []
        if msg["role"] == "assistant":
            text = "".join(b.get("text", "") for b in blocks)
            calls = [{"id": b["toolUse"]["toolUseId"], "type": "function",
                      "function": {"name": b["toolUse"]["name"], "arguments": json.dumps(b["toolUse"].get("input") or {})}}
                     for b in blocks if "toolUse" in b]
            m: dict = {"role": "assistant", "content": text or None}
            if calls:
                m["tool_calls"] = calls
            out.append(m)
            continue
        extra: list[dict] = []  # images in tool results, and the user's own blocks: after the tool messages
        for b in blocks:
            if "toolResult" in b:
                r = b["toolResult"]
                texts, images = [], []
                for c in r.get("content") or []:
                    p = _part(c)
                    if p is None:
                        continue
                    (images if p["type"] != "text" else texts).append(p)
                body = "\n".join(t["text"] for t in texts) or ("(see the attached content)" if images else "(empty)")
                if r.get("status") == "error":
                    body = f"[error] {body}"
                out.append({"role": "tool", "tool_call_id": r["toolUseId"], "content": body})
                if images:
                    extra.append({"type": "text", "text": f"[content returned by tool call {r['toolUseId']}]"})
                    extra.extend(images)
            else:
                p = _part(b)
                if p:
                    extra.append(p)
        if extra:
            out.append({"role": "user", "content": extra})
    return out


def to_tools(tool_config: dict | None) -> list[dict] | None:
    if not tool_config:
        return None
    return [{"type": "function", "function": {"name": t["toolSpec"]["name"],
                                              "description": t["toolSpec"].get("description", ""),
                                              "parameters": t["toolSpec"]["inputSchema"]["json"]}}
            for t in tool_config["tools"] if "toolSpec" in t]


def from_openai(data: dict, model_id: str, tier: str) -> ModelReply:
    if data.get("error"):
        raise RuntimeError(f"OpenRouter: {data['error'].get('message') or data['error']}")
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("content"):
        content.append({"text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_unparsed_arguments": fn.get("arguments")}
        content.append({"toolUse": {"toolUseId": call.get("id") or f"call_{len(content)}", "name": fn.get("name"),
                                    "input": args if isinstance(args, dict) else {"value": args}}})
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = Usage(model_id=model_id, tier=tier, input_tokens=(u.get("prompt_tokens") or 0) - cached,
                  output_tokens=u.get("completion_tokens") or 0, cache_read_tokens=cached,
                  cost_usd=float(u.get("cost") or 0.0))
    stop = STOP.get(choice.get("finish_reason") or "", choice.get("finish_reason") or "")
    if any("toolUse" in b for b in content):
        stop = "tool_use"
    return ModelReply(content=content, stop_reason=stop, usage=usage)


class OpenRouterClient:
    def __init__(self, settings, *, http: httpx.AsyncClient | None = None):
        self.s = settings
        key = os.environ.get(settings.api_key_env, "")
        if not key:
            raise RuntimeError(f"OpenRouter is configured but {settings.api_key_env} is not set")
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        self.headers = {"Authorization": f"Bearer {key}", "X-Title": settings.app_name}

    def body(self, *, model_id, system, messages, tool_config, max_tokens) -> dict:
        body: dict[str, Any] = {
            "model": model_id, "max_tokens": max_tokens, "usage": {"include": True},
            "messages": to_openai(system, messages, cache=model_id.startswith("anthropic/")),
        }
        tools = to_tools(tool_config)
        if tools:
            body["tools"] = tools
        provider: dict[str, Any] = {"data_collection": self.s.data_collection}
        if self.s.zdr:
            provider["zdr"] = True
        if self.s.providers:
            provider["order"] = self.s.providers
            provider["allow_fallbacks"] = self.s.allow_fallbacks
        body["provider"] = provider
        return body

    async def converse(self, *, model_id, tier, system, messages, tool_config, max_tokens) -> ModelReply:
        body = self.body(model_id=model_id, system=system, messages=messages, tool_config=tool_config,
                         max_tokens=max_tokens)
        url = f"{self.s.base_url.rstrip('/')}/chat/completions"
        t0 = time.monotonic()
        for attempt in range(4):
            r = await self.http.post(url, json=body, headers=self.headers)
            if r.status_code not in RETRY_STATUS or attempt == 3:
                break
            await asyncio.sleep(min(2 ** attempt, 8))
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", {}).get("message", "")
            except ValueError:
                detail = r.text[:200]
            raise RuntimeError(f"OpenRouter {r.status_code}: {detail}")
        reply = from_openai(r.json(), model_id, tier)
        reply.latency_ms = round((time.monotonic() - t0) * 1000)
        return reply


class ProviderRouter:
    """The model client for an agent: each model id goes to the provider its tier names (Bedrock by default)."""

    def __init__(self, bedrock, openrouter, openrouter_models: set[str]):
        self.bedrock, self.openrouter, self.openrouter_models = bedrock, openrouter, openrouter_models

    async def converse(self, *, model_id, **kw) -> ModelReply:
        client = self.openrouter if model_id in self.openrouter_models else self.bedrock
        return await client.converse(model_id=model_id, **kw)


def model_client(models_cfg):
    """Bedrock, OpenRouter, or both, as the agent's tiers say."""
    from .bedrock import BedrockClient
    routed = {t.model_id for t in models_cfg.tiers.values() if t.provider == "openrouter"}
    bedrock_ids = {t.model_id for t in models_cfg.tiers.values() if t.provider == "bedrock"}
    if not routed:
        return BedrockClient(models_cfg.region, models_cfg.prompt_caching)
    openrouter = OpenRouterClient(models_cfg.openrouter)
    if not bedrock_ids:
        return openrouter
    return ProviderRouter(BedrockClient(models_cfg.region, models_cfg.prompt_caching), openrouter, routed)
