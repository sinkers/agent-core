"""Test doubles for agents built on the core: a scripted model, a fake MCP server and an agent wired from them.

A deployment's own tests (its agent configs, skills and brand) use these too:

    from agentcore.testing import FakeModel, FakeMcp, make_agent
    agent, model, store, tools = make_agent(cfg, ["Hello."], skills_dir="skills")
"""

from __future__ import annotations

import os
from pathlib import Path

from .agent import Agent
from .bedrock import ModelReply
from .budget import BudgetTracker
from .config import PRICING_DEFAULT, load_pricing
from .killswitch import KillSwitch
from .skills import SkillLoader
from .store import MemoryStore, Usage
from .tools import ToolGateway


class FakeModel:
    """Scripted Bedrock stand-in. Each script item is text, or a list of (tool, args) calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def converse(self, *, model_id, tier, system, messages, tool_config, max_tokens):
        self.calls.append({"model_id": model_id, "tier": tier, "system": system, "messages": list(messages),
                           "tool_config": tool_config})
        item = self.script.pop(0) if self.script else "done"
        if messages and messages[-1]["role"] == "assistant" and isinstance(item, str) and item.startswith("{"):
            item = item[1:]  # a real model continues after the prefilled "{"
        usage = Usage(model_id=model_id, tier=tier, input_tokens=1000, output_tokens=100)
        if isinstance(item, list):
            content = [{"toolUse": {"toolUseId": f"t{i}", "name": n, "input": a}} for i, (n, a) in enumerate(item)]
            return ModelReply(content, "tool_use", usage)
        if isinstance(item, tuple):  # ("text", "max_tokens"): a reply cut off at the length limit
            return ModelReply([{"text": item[0]}] if item[0] else [], item[1], usage)
        return ModelReply([{"text": item}], "end_turn", usage)


class FakeMcp:
    def __init__(self, tools: list[str]):
        self.tools = tools
        self.called: list[tuple[str, dict]] = []

    async def list_tools(self):
        return [{"name": t, "description": t, "inputSchema": {"type": "object"}} for t in self.tools]

    async def call_tool(self, name, args):
        self.called.append((name, args))
        return f"result of {name}"


def make_agent(cfg, script, mcp: FakeMcp | None = None, killswitch_state: str = "run", triage: bool = False, *,
               skills_dir: str | Path, pricing_file: str | Path | None = None):
    """An Agent on a FakeModel and an in-memory store; `mcp` stands in for the `openclaw` MCP server."""
    cfg.models.triage = triage  # the first responder adds a model call, so tests opt in explicitly
    os.environ["KILLSWITCH_OVERRIDE"] = killswitch_state
    store = MemoryStore()
    skills = SkillLoader(skills_dir, cfg.agent.name, cfg.skills.always, cfg.skills.on_demand)
    tools = ToolGateway(cfg, skills, {"openclaw": mcp} if mcp else {})
    ks = KillSwitch(cfg.agent.name)
    ks.apply({cfg.agent.name: killswitch_state})
    model = FakeModel(script)
    agent = Agent(cfg, model=model, store=store, budget=BudgetTracker(cfg.budget, store, load_pricing(pricing_file or PRICING_DEFAULT)),
                  tools=tools, skills=skills, killswitch=ks, persona="You are a test agent.")
    return agent, model, store, tools
