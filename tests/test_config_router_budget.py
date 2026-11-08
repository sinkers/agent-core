from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agentcore.budget import BudgetTracker, cost_usd
from agentcore.config import AgentConfig, load_agent_config, load_pricing
from agentcore.router import ModelRouter, TaskContext, split_confidence
from agentcore.store import MemoryStore, Usage

from .conftest import AGENTS, PRICING


@pytest.mark.parametrize("name", ["ada", "eve", "dan"])
def test_every_agent_config_loads_and_is_priced(name):
    cfg = load_agent_config(AGENTS / name / "agent.yaml")
    prices = load_pricing(PRICING)
    assert cfg.agent.name == name
    assert cfg.agent.display_name == f"{name.capitalize()} (AI)"
    assert {"fast", "default", "deep"} <= set(cfg.models.tiers) <= {"fast", "default", "deep", "frontier"}
    for tier in cfg.models.tiers.values():
        assert tier.model_id in prices, f"{tier.model_id} has no price"
    assert "AI agent" in cfg.footer and "A. Owner" in cfg.footer  # PLT-004


def test_tool_must_reference_known_server(cfg):
    raw = cfg.model_dump()
    raw["tools"]["allow"].append({"server": "nope", "name": "x"})
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(raw)


def test_standing_rule_must_be_allow_listed(cfg):
    raw = cfg.model_dump()
    raw["tools"]["standing_rules"] = ["openclaw__apps_delete"]  # not allow-listed
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(raw)


@pytest.mark.parametrize("text,reason", [
    ("This is urgent, where is PRJ up to?", "user_urgency"),
    ("Please review PRJ-GEN-REQ-0001", "class1_document"),
    ("Check the earthing calc", "class1_document"),
])
def test_escalates_to_deep(cfg, text, reason):
    d = ModelRouter(cfg.models).decide(TaskContext(text=text))
    assert d.tier == "deep" and d.escalated
    assert any(r.startswith(reason) for r in d.reasons)


def test_tool_steps_and_low_confidence_escalate(cfg):
    r = ModelRouter(cfg.models)
    assert r.decide(TaskContext(text="hi", tool_steps=9)).tier == "deep"
    assert r.decide(TaskContext(text="hi", tool_steps=8)).tier == "default"
    assert r.decide(TaskContext(text="hi", low_confidence=True)).reasons == ["low_confidence"]


def test_plain_request_stays_default_and_fast_never_escalates(cfg):
    r = ModelRouter(cfg.models)
    assert r.decide(TaskContext(text="hello")).tier == "default"
    assert r.decide(TaskContext(text="urgent!", requested_tier="fast")).tier == "fast"


def test_split_confidence():
    assert split_confidence("Answer.\n[confidence: low]") == ("Answer.", "low")
    assert split_confidence("Answer.") == ("Answer.", None)


def test_cost_usd():
    price = load_pricing(PRICING)["au.anthropic.claude-haiku-4-5-20251001-v1:0"]
    u = Usage("m", "fast", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd(price, u) == pytest.approx(6.60)


async def test_budget_alerts_once_at_80_and_100(cfg):
    store = MemoryStore()
    bt = BudgetTracker(cfg.budget, store, load_pricing(PRICING))
    now = datetime.now(timezone.utc)
    await store.record_usage(Usage("m", "default", cost_usd=cfg.budget.daily_usd * 0.85))
    first = await bt.alerts_due(now)
    assert len(first) == 1 and "daily" in first[0] and "80%" in first[0]
    assert await bt.alerts_due(now) == []  # not repeated
    await store.record_usage(Usage("m", "default", cost_usd=cfg.budget.daily_usd * 0.2))
    assert (await bt.state(now)).exhausted
    assert any("reached" in a for a in await bt.alerts_due(now))


def test_unknown_model_price_refused(cfg):
    bt = BudgetTracker(cfg.budget, MemoryStore(), {})
    with pytest.raises(KeyError):
        bt.price_for("mystery-model")


async def test_budget_override_changes_limits_without_a_restart(cfg):
    store = MemoryStore()
    bt = BudgetTracker(cfg.budget, store, load_pricing(PRICING))
    now = datetime.now(timezone.utc)
    await store.record_usage(Usage("m", "default", cost_usd=cfg.budget.daily_usd))
    assert (await bt.state(now)).exhausted

    await bt.set_override(cfg.budget.daily_usd * 2, None, "alex", now)
    st = await bt.state(now)
    assert not st.exhausted and st.day_pct == pytest.approx(0.5)
    assert st.daily_limit_usd == cfg.budget.daily_usd * 2 and st.monthly_limit_usd == cfg.budget.monthly_usd

    o = await bt.set_override(None, 9_000, "alex", now)  # keeps the daily override
    assert o["daily_usd"] == cfg.budget.daily_usd * 2 and o["monthly_usd"] == 9_000
    with pytest.raises(ValueError):
        await bt.set_override(0, None, "alex", now)

    assert await bt.set_override(None, None, "alex", now) == {}
    assert (await bt.state(now)).exhausted  # back to agent.yaml


async def test_raised_limit_alerts_again_at_its_own_80_percent(cfg):
    store = MemoryStore()
    bt = BudgetTracker(cfg.budget, store, load_pricing(PRICING))
    now = datetime.now(timezone.utc)
    await store.record_usage(Usage("m", "default", cost_usd=cfg.budget.daily_usd * 0.85))
    assert len(await bt.alerts_due(now)) == 1
    await bt.set_override(cfg.budget.daily_usd * 2, None, "alex", now)
    assert await bt.alerts_due(now) == []  # 42% of the new limit
    await store.record_usage(Usage("m", "default", cost_usd=cfg.budget.daily_usd * 0.8))
    assert any("80%" in a for a in await bt.alerts_due(now))


def test_budget_endpoint_takes_a_json_body(monkeypatch):
    """The request model must resolve, or FastAPI treats it as a query parameter (422 on every PUT)."""
    from pathlib import Path

    monkeypatch.setenv("AGENT_DIR", str(Path(__file__).resolve().parent / "fixtures" / "agents" / "dan"))
    from agentcore.app import create_app

    route = next(r for r in create_app().routes if getattr(r, "path", "") == "/internal/budget")
    assert [p.name for p in route.dependant.body_params] == ["change"]
    assert not [p.name for p in route.dependant.query_params]
