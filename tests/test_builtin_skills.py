"""The core skill pack (plugins/agent-skills), and how it sits beside a deployment's own skills."""

import pytest

from agentcore.skills import BUILTIN_SKILLS, SkillLoader, parse_skill


def builtin():
    return sorted(p.parent.name for p in BUILTIN_SKILLS.glob("*/SKILL.md"))


def test_every_builtin_skill_parses_and_applies_to_any_agent():
    assert {"escalation", "vector-art"} <= set(builtin())
    for name in builtin():
        s = parse_skill(BUILTIN_SKILLS / name / "SKILL.md")
        assert s.name == name and s.description and s.version and s.applies_to == ("*",)


def test_an_agent_loads_a_builtin_skill_with_its_placeholders_filled(tmp_path):
    loader = SkillLoader(tmp_path, "anyone", [], ["escalation"])
    text = loader.load("escalation")
    assert "outside Acme" in text and "{org}" not in text


def test_a_deployment_skill_of_the_same_name_wins(tmp_path):
    (tmp_path / "escalation").mkdir()
    (tmp_path / "escalation" / "SKILL.md").write_text(
        '---\nname: escalation\ndescription: "Ours."\nversion: 9.0.0\napplies_to: [ada]\n---\nOur way of chasing.')
    loader = SkillLoader(tmp_path, "ada", [], ["escalation", "vector-art"])
    assert loader.skills["escalation"].version == "9.0.0" and loader.skills["vector-art"].applies_to == ("*",)


def test_several_skill_folders_are_searched_in_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, body in ((a, "from a"), (b, "from b")):
        (d / "notes").mkdir(parents=True)
        (d / "notes" / "SKILL.md").write_text(f'---\nname: notes\ndescription: "N."\nversion: 1.0.0\napplies_to: ["*"]\n---\n{body}')
    import os
    assert "from a" in SkillLoader(f"{a}{os.pathsep}{b}", "ada", ["notes"], []).system_prompt_block()
    with pytest.raises(FileNotFoundError):
        SkillLoader(a, "ada", ["missing"], [])


def write(folder, name, front, body):
    (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / name / "SKILL.md").write_text(f"---\nname: {name}\n{front}\n---\n{body}")


def test_a_deployment_skill_that_extends_core_adds_to_it(tmp_path):
    write(tmp_path, "escalation", 'description: "Chasing, Acme style."\nversion: 1.0.0\napplies_to: [ada]\nextends: core',
          "## Acme rules\n\nChase finance on Tuesdays only.")
    (tmp_path / "escalation" / "references").mkdir()
    (tmp_path / "escalation" / "references" / "acme-contacts.md").write_text("Finance: {org} finance team")
    loader = SkillLoader(tmp_path, "ada", ["escalation"], [])
    s = loader.skills["escalation"]
    core = parse_skill(BUILTIN_SKILLS / "escalation" / "SKILL.md")
    assert s.body.startswith(core.body) and s.body.endswith("Chase finance on Tuesdays only.")
    assert s.version == f"{core.version}+1.0.0" and s.applies_to == ("ada",) and s.description == "Chasing, Acme style."
    assert "references/acme-contacts.md" in loader.references("escalation")
    assert "Acme finance team" in loader.load_reference("escalation", "acme-contacts")


def test_extending_a_skill_the_core_does_not_have_is_an_error(tmp_path):
    write(tmp_path, "nothing-like-it", 'description: "x"\nversion: 1.0.0\napplies_to: [ada]\nextends: core', "x")
    with pytest.raises(ValueError, match="no core skill"):
        SkillLoader(tmp_path, "ada", ["nothing-like-it"], [])

