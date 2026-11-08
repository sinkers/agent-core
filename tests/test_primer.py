import base64

from agentcore.primer import PrimerConfig, PrimerSync
from agentcore.skills import SkillLoader
from agentcore.store import MemoryStore

from .conftest import SKILLS

FILES = {
    "00-index.md": "---\nname: ada-index\ndescription: Entry point.\nversion: 0.1\n---\n# Ada memory primer\nRead first.",
    "10-context.md": "---\nname: context\ndescription: Who Acme is.\n---\nAcme runs PRJ George Town.",
    "30-prince2.md": "---\nname: prince2\ndescription: PRINCE2 7 primer. Read for any PRINCE2 question.\n---\nTolerances: time, cost, scope...",
}


class FakeOpenclaw:
    def __init__(self, files, fail=False):
        self.files, self.fail, self.calls = files, fail, []

    async def call_tool(self, name, args):
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("MCP down")
        fname = args["path"].rsplit("/", 1)[-1]
        b64 = base64.b64encode(self.files[fname].encode()).decode()
        return f"**name:** {fname}\n**size:** {len(self.files[fname])} bytes\n**download_url:** https://x?tempauth=s\n\n**content_base64:**\n{b64}"


def cfg():
    return PrimerConfig(site="https://acme.sharepoint.com/sites/3.0Projects", folder="0. PMO Framework/ada-memory",
                        always=["00-index.md", "10-context.md"], on_demand=["30-prince2.md"])


async def test_primer_loads_always_into_prompt_and_on_demand_as_skills():
    skills = SkillLoader(SKILLS, "ada", [], [])
    sync = PrimerSync(cfg(), skills, FakeOpenclaw(FILES), MemoryStore(), agent="ada")
    assert await sync.refresh() == 3
    block = skills.system_prompt_block()
    assert "Ada memory primer" in block and "Acme runs PRJ" in block
    assert "Tolerances" not in block and "prince2: PRINCE2 7 primer" in block  # on demand: named, not loaded
    assert "Tolerances" in skills.load("prince2")


async def test_primer_falls_back_to_last_good_copy_when_sharepoint_is_down():
    store = MemoryStore()
    skills = SkillLoader(SKILLS, "ada", [], [])
    await PrimerSync(cfg(), skills, FakeOpenclaw(FILES), store, agent="ada").refresh()
    cold = SkillLoader(SKILLS, "ada", [], [])
    assert await PrimerSync(cfg(), cold, FakeOpenclaw(FILES, fail=True), store, agent="ada").refresh() == 0
    assert "Ada memory primer" in cold.system_prompt_block()  # restored from the stored copy
