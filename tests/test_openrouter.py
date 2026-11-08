"""OpenRouter as a model provider (openrouter.py): Converse messages to the OpenAI-style API and back, routing by
tier, costs from OpenRouter's own figure."""

from __future__ import annotations

import json

import httpx
import pytest

from agentcore.bedrock import ModelReply
from agentcore.budget import BudgetTracker
from agentcore.config import ModelsConfig, OpenRouterSettings, family_of
from agentcore.openrouter import OpenRouterClient, ProviderRouter, from_openai, model_client, to_openai, to_tools
from agentcore.store import MemoryStore, Usage

TOOLS = {"tools": [{"toolSpec": {"name": "jira__get_issue", "description": "Get an issue",
                                 "inputSchema": {"json": {"type": "object", "properties": {"key": {"type": "string"}}}}}},
                   {"cachePoint": {"type": "default"}}]}


def test_messages_tool_calls_and_images_translate():
    messages = [
        {"role": "user", "content": [{"text": "What's in PRJ-1?"}]},
        {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "hmm"}}},
                                          {"text": "Looking."},
                                          {"toolUse": {"toolUseId": "t1", "name": "jira__get_issue",
                                                       "input": {"key": "PRJ-1"}}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "PRJ-1: fix the gate"},
                                                           {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}}]}},
            {"text": "and the drawing?"}]},
    ]
    out = to_openai(["You are Ada.", "", "Skills."], messages, cache=True)
    assert out[0] == {"role": "system", "content": [{"type": "text", "text": "You are Ada.\n\nSkills.",
                                                     "cache_control": {"type": "ephemeral"}}]}
    assert out[1] == {"role": "user", "content": [{"type": "text", "text": "What's in PRJ-1?"}]}
    assert out[2]["role"] == "assistant" and out[2]["content"] == "Looking."  # another model's reasoning dropped
    assert out[2]["tool_calls"][0]["function"] == {"name": "jira__get_issue", "arguments": '{"key": "PRJ-1"}'}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "PRJ-1: fix the gate"}
    # the tool's image and the user's words follow the tool message, as a user message
    assert out[4]["role"] == "user"
    assert out[4]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out[4]["content"][-1] == {"type": "text", "text": "and the drawing?"}
    assert to_tools(TOOLS) == [{"type": "function", "function": {
        "name": "jira__get_issue", "description": "Get an issue",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}}}}}]


def test_reply_with_tool_calls_and_cost():
    data = {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "Checking.", "tool_calls": [{"id": "c1", "type": "function",
                                                "function": {"name": "jira__get_issue", "arguments": '{"key":"PRJ-2"}'}}]}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "cost": 0.0042,
                      "prompt_tokens_details": {"cached_tokens": 1000}}}
    r = from_openai(data, "anthropic/claude-sonnet-4.5", "default")
    assert r.text == "Checking." and r.stop_reason == "tool_use"
    assert r.tool_uses == [{"toolUseId": "c1", "name": "jira__get_issue", "input": {"key": "PRJ-2"}}]
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (200, 1000, 40)
    assert r.usage.cost_usd == pytest.approx(0.0042)
    with pytest.raises(RuntimeError, match="rate limited"):
        from_openai({"error": {"message": "rate limited"}}, "m", "fast")


async def test_client_sends_privacy_settings_and_retries(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-test"
        if len(seen) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "pong"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.00001}})
    monkeypatch.setattr("agentcore.openrouter.asyncio.sleep", _no_sleep)
    client = OpenRouterClient(OpenRouterSettings(zdr=True, providers=["anthropic"], allow_fallbacks=False),
                              http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await client.converse(model_id="openai/gpt-5-mini", tier="fast", system=["Be brief."],
                              messages=[{"role": "user", "content": [{"text": "ping"}]}], tool_config=None,
                              max_tokens=50)
    assert r.text == "pong" and len(seen) == 2
    body = seen[-1]
    assert body["provider"] == {"data_collection": "deny", "zdr": True, "order": ["anthropic"], "allow_fallbacks": False}
    assert body["usage"] == {"include": True} and "cache_control" not in json.dumps(body)  # not an Anthropic model


async def _no_sleep(_):
    return None


def test_tiers_choose_the_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    tiers = {"fast": {"model_id": "google/gemini-2.5-flash", "provider": "openrouter"},
             "default": {"model_id": "au.anthropic.claude-sonnet-5"},
             "deep": {"model_id": "au.anthropic.claude-opus-5-5"}}
    client = model_client(ModelsConfig.model_validate({"tiers": tiers}))
    assert isinstance(client, ProviderRouter) and client.openrouter_models == {"google/gemini-2.5-flash"}
    plain = model_client(ModelsConfig.model_validate({"tiers": {t: {"model_id": "au.anthropic.claude-sonnet-5"}
                                                                for t in ("fast", "default", "deep")}}))
    assert type(plain).__name__ == "BedrockClient"
    assert family_of("anthropic/claude-sonnet-4.5") == "anthropic" and family_of("x-ai/grok-4") == "xai"
    assert family_of("au.anthropic.claude-sonnet-5") == "anthropic"


async def test_router_sends_each_model_to_its_provider():
    class Fake:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def converse(self, **kw):
            self.calls.append(kw["model_id"])
            return ModelReply([{"text": self.name}], "end_turn", Usage(kw["model_id"], kw["tier"]))
    b, o = Fake("bedrock"), Fake("openrouter")
    router = ProviderRouter(b, o, {"google/gemini-2.5-flash"})
    r = await router.converse(model_id="google/gemini-2.5-flash", tier="fast", system=[], messages=[],
                              tool_config=None, max_tokens=10)
    assert r.text == "openrouter" and o.calls == ["google/gemini-2.5-flash"] and not b.calls


async def test_budget_takes_the_reported_cost_and_still_refuses_unpriced_models(cfg):
    budget = BudgetTracker(cfg.budget, MemoryStore(), prices={})
    u = await budget.record(Usage("google/gemini-2.5-flash", "fast", 100, 10, cost_usd=0.0007))
    assert u.cost_usd == pytest.approx(0.0007)
    with pytest.raises(KeyError):
        await budget.record(Usage("google/gemini-2.5-flash", "fast", 100, 10))  # no cost reported, no price
