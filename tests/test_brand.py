"""Acme-branded decks and one-pagers, checked against the fact register."""

import io
import json
import os

from agentcore.brand import BrandTools
from agentcore.facts import FactTools
from agentcore.files import FileTools
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

APPROVER = CallContext("c", "aad-alex", [], actor_name="A. Owner")


async def setup(tmp_path):
    store = MemoryStore()
    facts = FactTools(store, [{"name": "A. Owner", "aad_id": "aad-alex"}])
    f = json.loads(await facts.call_tool("propose_fact", {"statement": "10 MW site energised in 14-18 months",
                                                          "area": "build", "value": "14-18", "unit": "months",
                                                          "source": "REQ-001 E"}, APPROVER))["fact"]["id"]
    await facts.call_tool("approve_fact", {"fact_id": f}, APPROVER)
    files = FileTools(store)
    return BrandTools(files=files, facts=facts), files, f


async def test_unsupported_claims_mean_not_customer_ready(tmp_path):
    brand, files, f = await setup(tmp_path)
    out = json.loads(await brand.call_tool("make_one_pager", {"title": "GPU hall", "language": "ja", "sections": [
        {"heading": "Uptime", "text": "99.99% uptime guaranteed"}]}, CallContext("chat-c", "u", [])))
    assert not out["customer_ready"] and "99.99%" in out["claim_check"]["unsupported_numbers"]
    assert out["name"].endswith(".docx") and "native speaker" in out["translation_note"]
