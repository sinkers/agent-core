"""Skill loader.

Skills listed under `skills.always` are injected into the system prompt. Skills under
`skills.on_demand` are only named in the prompt and fetched with the built-in
`skills__load` tool. A skill is only visible to an agent whose name is in the skill's
`applies_to` front matter ("*": any agent). Agents never write skills: this module is read-only.

Skills are looked up in the deployment's folders (SKILLS_DIR, several joined with os.pathsep), then in the core's
skill pack (plugins/agent-skills in the source, builtin_skills/ in the package); the first folder holding a skill
of that name wins. A deployment skill with `extends: core` in its front matter adds to the core skill of the same
name instead of replacing it: the agent gets the core text, then the deployment's rules, and both skills'
references. `{org}`, `{place}` and the other org.localise placeholders in a skill's text are filled from org.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .org import localise

# The core's skill pack: packaged into the wheel as builtin_skills/ (pyproject force-include); in a source checkout
# (editable install, tests) it is read from plugins/agent-skills/skills.
_PACKAGED = Path(__file__).with_name("builtin_skills")
BUILTIN_SKILLS = _PACKAGED if _PACKAGED.is_dir() else Path(__file__).resolve().parents[2] / "plugins" / "agent-skills" / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    applies_to: tuple[str, ...]
    body: str
    extends: str = ""  # "core": adds to the core skill of the same name


def parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing front matter")
    return parse_skill_text(text)


def parse_skill_text(text: str, default_name: str | None = None) -> Skill:
    """A skill from Markdown with YAML front matter (name, description, version, applies_to)."""
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        if default_name is None:
            raise ValueError("missing front matter")
        return Skill(name=default_name, description="", version="", applies_to=(), body=text.strip())
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    return Skill(
        name=meta.get("name") or default_name,
        description=localise(str(meta.get("description", "")).strip()),
        version=str(meta.get("version", "")),
        applies_to=tuple(meta.get("applies_to") or ()),
        body=localise(body.strip()),
        extends=str(meta.get("extends") or ""),
    )


def combine(base: Skill, extra: Skill) -> Skill:
    """A deployment's additions (extends: core) on top of the core skill: core text first, then the additions."""
    return Skill(name=base.name, description=extra.description or base.description,
                 version=f"{base.version}+{extra.version}" if extra.version else base.version,
                 applies_to=extra.applies_to or base.applies_to, body=f"{base.body}\n\n{extra.body}")


def skill_dirs(skills_dir: str | Path | list) -> list[Path]:
    """The folders to look in, in order: the deployment's, then the core's built-in skills."""
    given = skills_dir if isinstance(skills_dir, (list, tuple)) else str(skills_dir).split(os.pathsep)
    return [*(Path(d) for d in given if str(d)), BUILTIN_SKILLS]


class SkillLoader:
    def __init__(self, skills_dir: str | Path, agent: str, always: list[str], on_demand: list[str]):
        self.agent = agent
        self.dirs = skill_dirs(skills_dir)
        self.dir = self.dirs[0]
        self.folders: dict[str, list[Path]] = {}  # name -> the folders its text came from (deployment first)
        self.skills: dict[str, Skill] = {}
        wanted = list(dict.fromkeys([*always, *on_demand]))
        for name in wanted:
            found = [d / name for d in self.dirs if (d / name / "SKILL.md").exists()]
            if not found:
                raise FileNotFoundError(f"skill {name} configured for {agent} not found in "
                                        f"{', '.join(str(d) for d in self.dirs)}")
            skill, folders = parse_skill(found[0] / "SKILL.md"), [found[0]]
            if skill.extends:
                if skill.extends != "core" or len(found) < 2:
                    raise ValueError(f"skill {name} extends {skill.extends!r}, but there is no core skill {name}")
                skill, folders = combine(parse_skill(found[1] / "SKILL.md"), skill), [found[0], found[1]]
            if "*" not in skill.applies_to and agent not in skill.applies_to:
                raise ValueError(f"skill {name} does not list {agent} in applies_to")
            self.skills[name] = skill
            self.folders[name] = folders
        self.always = [n for n in always if n in self.skills]
        self.on_demand = [n for n in on_demand if n in self.skills and n not in self.always]
        self.dynamic: dict[str, str] = {}  # name -> source, for skills loaded at run time (primer.py)

    def set_dynamic(self, always: list[Skill], on_demand: list[Skill], source: str) -> None:
        """Replace the run-time skills from `source` (e.g. the SharePoint primer) with a fresh set."""
        for name in [n for n, src in self.dynamic.items() if src == source]:
            self.skills.pop(name, None)
            self.always = [n for n in self.always if n != name]
            self.on_demand = [n for n in self.on_demand if n != name]
            del self.dynamic[name]
        for skill, kind in [*((s, "always") for s in always), *((s, "on_demand") for s in on_demand)]:
            if skill.name in self.skills:  # a repo skill of the same name wins
                continue
            self.skills[skill.name] = skill
            self.dynamic[skill.name] = source
            (self.always if kind == "always" else self.on_demand).append(skill.name)

    def system_prompt_block(self) -> str:
        parts: list[str] = []
        for name in self.always:
            s = self.skills[name]
            parts.append(f'<skill name="{s.name}" version="{s.version}">\n{s.body}\n</skill>')
        if self.on_demand:
            lines = "\n".join(f"- {n}: {self.skills[n].description}" for n in self.on_demand)
            parts.append(
                "Further skills are available on demand. Call skills__load with the skill name "
                f"before doing work the skill covers:\n{lines}"
            )
        return "\n\n".join(parts)

    def list(self) -> list[dict[str, str]]:
        out = []
        for s in self.skills.values():
            entry = {"name": s.name, "description": s.description, "version": s.version,
                     "loaded": "always" if s.name in self.always else "on_demand"}
            if refs := self.references(s.name):
                entry["references"] = refs  # skills__load with `reference` reads one
            out.append(entry)
        return out

    def load(self, name: str) -> str:
        if name not in self.skills:
            raise KeyError(f"unknown skill {name}; available: {sorted(self.skills)}")
        s = self.skills[name]
        return f'<skill name="{s.name}" version="{s.version}">\n{s.body}\n</skill>'

    def references(self, name: str) -> list[str]:
        """The reference files a repo skill carries (references/*.md), as paths relative to the skill folder."""
        if name not in self.skills or name in self.dynamic:
            return []
        files = {p.name for f in self._folders(name) if (f / "references").is_dir() for p in (f / "references").glob("*.md")}
        return sorted(f"references/{n}" for n in files)

    def _folders(self, name: str) -> list[Path]:
        return self.folders.get(name) or [self.dir / name]

    def load_reference(self, name: str, reference: str) -> str:
        """One of a skill's references/*.md files. Only files in that folder: no other paths."""
        if name not in self.skills:
            raise KeyError(f"unknown skill {name}; available: {sorted(self.skills)}")
        available = self.references(name)
        wanted = reference.strip().lstrip("./")
        wanted = wanted if wanted.startswith("references/") else f"references/{wanted}"
        if not wanted.endswith(".md"):
            wanted += ".md"
        if wanted not in available:
            raise KeyError(f"skill {name} has no reference {reference!r}; it has: {available or 'none'}")
        path = next(f / wanted for f in self._folders(name) if (f / wanted).is_file())  # the deployment's first
        text = localise(path.read_text(encoding="utf-8"))
        return f'<skill_reference skill="{name}" file="{wanted}">\n{text.strip()}\n</skill_reference>'
