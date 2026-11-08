"""Model router with three named tiers and the escalation rule."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import EscalationConfig, ModelsConfig, Tier, TierConfig

CONFIDENCE_RE = re.compile(r"\s*\[confidence:\s*(low|medium|high)\]\s*$", re.IGNORECASE)
DOC_NUMBER_RE = re.compile(r"\b[A-Z0-9]{2,8}-[A-Z]{2,4}-([A-Z]{3})-\d{4}\b")

# Appended to the system prompt so the `default` tier reports its own confidence.
CONFIDENCE_INSTRUCTION = (
    "End every final answer with a line of the form [confidence: low|medium|high] "
    "giving your honest confidence that the answer is correct and complete. "
    "This line is removed before the user sees your answer."
)


@dataclass
class TaskContext:
    text: str
    requested_tier: Tier | None = None
    tool_steps: int = 0
    low_confidence: bool = False
    extra_class1: bool = False  # set by agent logic that already knows the document class


@dataclass
class Decision:
    tier: Tier
    model: TierConfig
    escalated: bool = False
    reasons: list[str] = field(default_factory=list)


class ModelRouter:
    def __init__(self, models: ModelsConfig):
        self.models = models
        self.esc: EscalationConfig = models.escalation

    def tier(self, name: Tier) -> TierConfig:
        if name == "frontier" and "frontier" not in self.models.tiers:
            return self.models.tiers["deep"]
        return self.models.tiers[name]

    def escalation_reasons(self, ctx: TaskContext) -> list[str]:
        """Return why a `default` task must run on `deep`; empty list means it need not."""
        reasons: list[str] = []
        lowered = ctx.text.lower()
        for term in self.esc.urgency_terms:
            if re.search(rf"\b{re.escape(term.lower())}\b", lowered):
                reasons.append(f"user_urgency:{term}")
                break
        if ctx.extra_class1 or self._touches_class1(ctx.text):
            reasons.append("class1_document")
        if ctx.low_confidence:
            reasons.append("low_confidence")
        if ctx.tool_steps > self.esc.max_tool_steps:
            reasons.append(f"tool_steps>{self.esc.max_tool_steps}")
        return reasons

    def _touches_class1(self, text: str) -> bool:
        types = {t.upper() for t in self.esc.class1_doc_types}
        if any(m.group(1) in types for m in DOC_NUMBER_RE.finditer(text)):
            return True
        lowered = text.lower()
        return any(term.lower() in lowered for term in self.esc.class1_terms)

    def decide(self, ctx: TaskContext) -> Decision:
        base: Tier = ctx.requested_tier or "default"
        if base in ("deep", "frontier"):
            return Decision(tier=base, model=self.tier(base))
        if base != "default":
            # Only `default` escalates; `fast` (voice) and `deep` are explicit choices.
            return Decision(tier=base, model=self.tier(base))
        reasons = self.escalation_reasons(ctx)
        if reasons:
            return Decision(tier="deep", model=self.tier("deep"), escalated=True, reasons=reasons)
        return Decision(tier="default", model=self.tier("default"))


def split_confidence(text: str) -> tuple[str, str | None]:
    """Strip a trailing [confidence: x] marker. Returns (clean_text, level or None)."""
    m = CONFIDENCE_RE.search(text)
    if not m:
        return text, None
    return text[: m.start()].rstrip(), m.group(1).lower()
