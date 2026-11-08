"""Independent review models: a document drafted with one AI model is reviewed by a model from another maker.

A model reviewing its own kind of output tends to share its blind spots. Before a review, the reviewer
asks whether AI helped draft the document and which one; `choose_review_model` then picks the first
configured reviewer from a different maker (ModelsConfig.reviewers, in preference order) and pins it to
this conversation, so every model call for the rest of the review uses it (agent.py). The agent's own
deep tier counts as the in-house (Anthropic) reviewer, used when the author was another maker or a person.

In-process `review` tools:
  choose_review_model(generated_with, prefer?)   pin a reviewer for this conversation (6 hours)
  clear_review_model()                            back to the agent's normal models
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .config import ModelsConfig, ReviewerConfig, family_of

PIN_HOURS = 6
HOUSE = "house"

_FAMILIES = [
    ("anthropic", r"claude|anthropic|sonnet|opus|haiku|fable"),
    ("openai", r"chat\s*gpt|\bgpt|openai|copilot|codex|\bo[1-9]\b"),
    ("google", r"gemini|bard|google|gemma"),
    ("meta", r"llama|meta ai"),
    ("mistral", r"mistral|le chat"),
    ("deepseek", r"deepseek"),
    ("xai", r"grok|xai"),
    ("amazon", r"nova|amazon q"),
    ("moonshot", r"kimi|moonshot"),
    ("qwen", r"qwen"),
]
_NONE = re.compile(r"\b(no|none|not used|didn'?t|did not|myself|by hand|human|nobody|no ai)\b", re.I)
_UNSURE = re.compile(r"\b(not sure|unsure|don'?t know|unknown|maybe|no idea|can'?t remember)\b", re.I)

TOOLS = [
    {"name": "choose_review_model",
     "description": ("Before reviewing a document, ask the person whether an AI model helped draft it and which one "
                     "(e.g. Claude, ChatGPT/Copilot, Gemini), then call this with their answer. It switches the rest "
                     "of this review to a model from a different maker, so the reviewer doesn't share the author's "
                     "blind spots, and returns which model to name in the review report. `prefer` may be 'best' to "
                     "use the strongest reviewer allowed."),
     "inputSchema": {"type": "object", "properties": {
         "generated_with": {"type": "string", "description": "what the person said, e.g. 'Claude', 'ChatGPT', 'no AI', 'not sure'"},
         "prefer": {"type": "string"}}, "required": ["generated_with"]}},
    {"name": "clear_review_model", "description": "Stop using the pinned review model in this conversation.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def author_family(said: str) -> str | None:
    """'anthropic' | 'openai' | … for the model the person named; None for no AI; 'unknown' if unsure."""
    text = said or ""
    for family, pattern in _FAMILIES:
        if re.search(pattern, text, re.I):
            return family
    if _UNSURE.search(text):
        return "unknown"
    if _NONE.search(text):
        return None
    return "unknown"


class ReviewModelTools:
    in_process = True

    def __init__(self, models: ModelsConfig, store, clock=None):
        self.models = models
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def list_tools(self) -> list[dict]:
        return TOOLS

    def candidates(self, prefer: str | None = None) -> list[tuple[str, ReviewerConfig]]:
        house_tier = self.models.tiers["deep"]
        house = ReviewerConfig(model_id=house_tier.model_id, max_tokens=house_tier.max_tokens,
                               label=f"in-house ({house_tier.model_id})")
        allowed = [(n, r) for n, r in self.models.reviewers.items()
                   if self.models.allow_global_reviewers or not r.global_routing]
        if prefer == "best":
            allowed.sort(key=lambda nr: not nr[1].global_routing)  # global (frontier) first when allowed
        return allowed + [(HOUSE, house)]

    def pick(self, author: str | None, prefer: str | None = None) -> tuple[str, ReviewerConfig]:
        cands = self.candidates(prefer)
        if author is None:  # written by a person: the strongest configured model from the agent's own maker
            own = family_of(self.models.tiers["default"].model_id)
            return next(((n, r) for n, r in cands if r.family == own), cands[-1])
        # Unknown author: Claude and ChatGPT are both common, so prefer a third maker, then anything not Anthropic.
        avoid = {author} if author != "unknown" else {"anthropic", "openai"}
        for name, r in cands:
            if r.family not in avoid:
                return name, r
        if author == "unknown":
            for name, r in cands:
                if r.family != family_of(self.models.tiers["default"].model_id):
                    return name, r
        return cands[-1]

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        conv = getattr(ctx, "conversation_id", None)
        if name == "clear_review_model":
            pin = await self.store.get_pref(conv, "review_model") if conv else None
            if pin:  # applies when this turn ends, so the write-up stays on the reviewing model (agent.py)
                await self.store.set_pref(conv, "review_model", {**pin, "clear_after_turn": True})
            return json.dumps({"cleared": True, "note": "the reviewing model finishes this reply, then stops"})
        if name != "choose_review_model":
            raise ValueError(f"unknown tool {name}")
        author = author_family(args.get("generated_with", ""))
        chosen, r = self.pick(author, args.get("prefer"))
        if conv:
            if chosen == HOUSE:
                await self.store.set_pref(conv, "review_model", None)
            else:
                await self.store.set_pref(conv, "review_model", {
                    "name": chosen, "model_id": r.model_id, "max_tokens": r.max_tokens, "author_family": author,
                    "until": (self.clock() + timedelta(hours=PIN_HOURS)).isoformat()})
        note = ("Unsure who drafted it, so a model from a third maker reviews it." if author == "unknown" else
                "Written without AI: reviewed by the normal model." if author is None else
                f"Drafted with {author}; reviewed by a {r.family} model instead.")
        return json.dumps({"reviewer": chosen, "reviewer_family": r.family, "model_id": r.model_id,
                           "label": r.label or r.model_id, "global_routing": r.global_routing,
                           "author_family": author, "note": note + " Name the reviewing model in the report."})


async def pinned_reviewer(store, conversation_id: str, now: datetime | None = None) -> dict | None:
    """The review model pinned to this conversation, if still current."""
    pin = await store.get_pref(conversation_id, "review_model")
    if not pin:
        return None
    if datetime.fromisoformat(pin["until"]) < (now or datetime.now(timezone.utc)):
        return None
    return pin


async def finish_turn(store, conversation_id: str) -> None:
    """End of a turn: drop a reviewer pin that was asked to clear."""
    pin = await store.get_pref(conversation_id, "review_model")
    if pin and pin.get("clear_after_turn"):
        await store.set_pref(conversation_id, "review_model", None)
