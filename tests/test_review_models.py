import json

import pytest

from agentcore.bedrock import BedrockClient
from agentcore.config import family_of, load_agent_config
from agentcore.review import ReviewModelTools, author_family
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

from .conftest import AGENTS, make_agent


@pytest.mark.parametrize("model_id,family", [
    ("au.anthropic.claude-sonnet-5", "anthropic"), ("global.openai.gpt-6.1-sol", "openai"),
    ("openai.gpt-oss-120b-1:0", "openai"), ("apac.amazon.nova-pro-v1:0", "amazon"), ("deepseek.v3.2", "deepseek"),
])
def test_family_of(model_id, family):
    assert family_of(model_id) == family


@pytest.mark.parametrize("said,family", [
    ("Claude", "anthropic"), ("I used Sonnet in Claude Code", "anthropic"), ("ChatGPT", "openai"),
    ("Microsoft Copilot", "openai"), ("gemini 3", "google"), ("no, I wrote it myself", None), ("not sure", "unknown"),
])
def test_author_family(said, family):
    assert author_family(said) == family


def eve():
    return load_agent_config(AGENTS / "eve" / "agent.yaml")


async def test_claude_authored_document_gets_a_non_anthropic_reviewer():
    cfg, store = eve(), MemoryStore()
    tools = ReviewModelTools(cfg.models, store)
    ctx = CallContext("conv-1", "aad-a", [])
    res = json.loads(await tools.call_tool("choose_review_model", {"generated_with": "Claude"}, ctx))
    assert res["reviewer_family"] == "openai" and res["model_id"] == "global.openai.gpt-6.1-sol"  # SOTA, not gpt-oss
    assert (await store.get_pref("conv-1", "review_model"))["name"] == res["reviewer"]
    unsure = json.loads(await tools.call_tool("choose_review_model", {"generated_with": "not sure"}, CallContext("c9", "a", [])))
    assert unsure["reviewer_family"] not in ("anthropic", "openai")  # a third maker


async def test_global_reviewers_are_skipped_when_not_allowed():
    cfg = eve()
    cfg.models.allow_global_reviewers = False
    tools = ReviewModelTools(cfg.models, MemoryStore())
    res = json.loads(await tools.call_tool("choose_review_model", {"generated_with": "Claude"}, CallContext("g", "a", [])))
    assert not res["global_routing"]


async def test_openai_authored_document_is_reviewed_by_claude_and_human_needs_no_change():
    cfg, store = eve(), MemoryStore()
    tools = ReviewModelTools(cfg.models, store)
    res = json.loads(await tools.call_tool("choose_review_model", {"generated_with": "ChatGPT"}, CallContext("c2", "a", [])))
    assert res["reviewer_family"] == "anthropic" and res["model_id"] == "global.anthropic.claude-fable-5-1"
    res = json.loads(await tools.call_tool("choose_review_model", {"generated_with": "no AI"}, CallContext("c3", "a", [])))
    assert res["reviewer_family"] == "anthropic" and res["model_id"] == "global.anthropic.claude-fable-5-1"


async def test_agent_switches_model_for_the_rest_of_the_review(cfg):
    eve_cfg = eve()
    agent, model, store, _ = make_agent(eve_cfg, ["Pass 1 findings..."], triage=True)
    tools = ReviewModelTools(eve_cfg.models, store)
    await tools.call_tool("choose_review_model", {"generated_with": "Claude"}, CallContext("rv", "a", []))
    await agent.handle(Turn("rv", "go ahead with the review of Acme-PRJ-REQ-0001"))
    reviewer = (await store.get_pref("rv", "review_model"))["model_id"]
    assert [c["model_id"] for c in model.calls] == [reviewer]  # no Haiku triage, straight to the reviewer
    await tools.call_tool("clear_review_model", {}, CallContext("rv", "a", []))
    assert (await store.get_pref("rv", "review_model"))["clear_after_turn"]  # still pinned until the turn ends
    from agentcore.review import finish_turn
    await finish_turn(store, "rv")
    assert await store.get_pref("rv", "review_model") is None


def test_no_cache_points_or_reasoning_blocks_for_non_anthropic_models():
    sent = {}

    class FakeBoto:
        def converse(self, **kw):
            sent.update(kw)
            return {"output": {"message": {"content": [{"reasoningContent": {"reasoningText": {"text": "hmm"}}},
                                                       {"text": "Finding 1"}]}},
                    "stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}}
    import asyncio
    client = BedrockClient.__new__(BedrockClient)
    client.region, client.prompt_caching, client._client = "ap-southeast-2", True, FakeBoto()
    tools = {"tools": [{"toolSpec": {"name": "t", "description": "d", "inputSchema": {"json": {}}}}]}
    reply = asyncio.run(client.converse(model_id="openai.gpt-oss-120b-1:0", tier="review", system=["sys"],
                                        messages=[], tool_config=tools, max_tokens=100))
    assert not any("cachePoint" in b for b in sent["system"]) and not any("cachePoint" in t for t in sent["toolConfig"]["tools"])
    assert reply.text == "Finding 1"  # reasoning is kept in content for the same model, never shown
    asyncio.run(client.converse(model_id="au.anthropic.claude-sonnet-5", tier="default", system=["sys"],
                                messages=[], tool_config=tools, max_tokens=100))
    assert any("cachePoint" in b for b in sent["system"])


from agentcore.agent import Turn  # noqa: E402


async def test_in_process_tools_reach_an_agent_with_no_external_servers():
    # Eve here has no MCP servers; an empty clients dict must still be shared with the gateway.
    from agentcore.skills import SkillLoader
    from agentcore.tools import ToolGateway

    from .conftest import SKILLS
    cfg = eve()
    clients: dict = {}
    gw = ToolGateway(cfg, SkillLoader(SKILLS, "eve", [], []), clients)
    clients["review"] = ReviewModelTools(cfg.models, MemoryStore())
    await gw.discover()
    assert "review__choose_review_model" in gw.specs


async def test_clearing_the_reviewer_waits_until_the_turn_is_finished():
    # The reviewer called clear_review_model before writing its answer, and the write-up silently moved to
    # the agent's normal model while still naming the reviewer. Clearing must apply after the turn.
    eve_cfg = eve()
    agent, model, store, mcp = make_agent(eve_cfg, [[("review__clear_review_model", {})], "Findings P3-01..."])
    tools = ReviewModelTools(eve_cfg.models, store)
    agent.tools.clients["review"] = tools
    await agent.tools.discover()
    await tools.call_tool("choose_review_model", {"generated_with": "Claude"}, CallContext("rv2", "a", []))
    await agent.handle(Turn("rv2", "do pass 3"))
    assert [c["model_id"] for c in model.calls] == ["global.openai.gpt-6.1-sol"] * 2  # write-up still on the reviewer
    assert await store.get_pref("rv2", "review_model") is None  # cleared once the turn ended
