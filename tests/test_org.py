"""The organisation seam (org.py): Acme's values come from org.yaml, the engine's own text carries placeholders."""

import importlib
import json
import pkgutil

import agentcore
import agentcore.org as org_mod
from agentcore.org import OrgConfig, localise


def test_placeholders_are_filled_and_other_braces_left_alone():
    text = "Ask {register_keeper} at {org} ({place} time): {title, bullets?: [..]} on {apps_host}"
    assert localise(text) == ("Ask Dan at Acme (Hobart time): {title, bullets?: [..]} on apps.example.com")
    assert localise({"a": ["{org}"], "b": ("{place}",)}) == {"a": ["Acme"], "b": ("Hobart",)}


def test_no_tool_description_is_left_with_a_placeholder():
    left = []
    for pkg in (agentcore, importlib.import_module("agentcore.plan")):
        for m in pkgutil.iter_modules(pkg.__path__):
            mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
            for attr in ("TOOLS", "SKILLS_TOOLS", "INSTRUCTIONS", "PAGE"):
                value = json.dumps(getattr(mod, attr, ""))
                left += [f"{mod.__name__}.{attr}: {p}" for p in ("{org}", "{place}", "{holidays_label}",
                         "{register_keeper}", "{apps_host}", "{library_scope}", "{method_doc}") if p in value]
    assert not left


def test_without_org_yaml_the_engine_is_neutral(monkeypatch):
    monkeypatch.setattr(org_mod, "org", lambda: OrgConfig())
    assert localise("{org}, {place}, {holidays_label}, {register_keeper}, {apps_host}") == (
        "Example, UTC, public holidays, the document controller, the apps site")
    neutral = OrgConfig()
    assert neutral.internal_domains == [] and neutral.agent_mailboxes == [] and str(neutral.tz) == "UTC"
