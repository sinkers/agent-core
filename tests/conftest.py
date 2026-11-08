from __future__ import annotations

import os
from pathlib import Path

import pytest

# The core's tests run as an example organisation (tests/fixtures/org.yaml) with the neutral built-in brand. A
# deployment's own settings, brand and agents are tested in its own repo, with agentcore.testing.
FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["ORG_FILE"] = str(FIXTURES / "org.yaml")
for _var in ("BRAND_DIR", "TEMPLATES_DIR"):
    os.environ.pop(_var, None)

from agentcore.config import load_agent_config
from agentcore.org import set_agent
from agentcore.testing import FakeMcp, FakeModel  # noqa: F401  (tests import them from here)
from agentcore.testing import make_agent as _make_agent

set_agent("Ada (AI)", "A. Owner")  # the agent most tests speak as


@pytest.fixture(autouse=True)
def _agent_identity():
    """Each test starts as Ada: starting another agent's app (create_app) changes the process's identity."""
    set_agent("Ada (AI)", "A. Owner")
    yield

CORE = Path(__file__).resolve().parents[1]
AGENTS = FIXTURES / "agents"
SKILLS = FIXTURES / "skills"
PRICING = CORE / "src" / "agentcore" / "pricing.yaml"


@pytest.fixture
def cfg():
    return load_agent_config(AGENTS / "ada" / "agent.yaml")


def make_agent(cfg, script, mcp: FakeMcp | None = None, killswitch_state: str = "run", triage: bool = False):
    return _make_agent(cfg, script, mcp, killswitch_state, triage, skills_dir=SKILLS, pricing_file=PRICING)
