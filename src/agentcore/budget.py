"""Per-agent USD budgets: alert the owner at 80%, hard stop at 100%.

The limits in agent.yaml are the defaults. An override set at run time (PUT /internal/budget, or
scripts/agents-budget.sh) is kept in the agent's database and wins until it is cleared: no build or restart.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import BudgetConfig, Price
from .store import Store, Usage
from .org import tz
from .org import agent

# Who, where and which session the current turn is for (agent.py sets it per turn): every model call records it,
# so spend can be reported per agent, person, channel and session.
USAGE_META: contextvars.ContextVar[dict] = contextvars.ContextVar("usage_meta", default={})

# Budget periods follow the working day in the local time zone, not UTC.
TZ = tz()
def over_budget_reply() -> str:
    return f"I'm over budget, ask {agent().owner_first}."
# The run-time override lives in the prefs table under this owner, not under any person.
OVERRIDE_OWNER, OVERRIDE_KEY = "_agent", "budget_override"


def cost_usd(price: Price, usage: Usage) -> float:
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / 1_000_000


@dataclass
class BudgetState:
    day_usd: float
    month_usd: float
    day_pct: float
    month_pct: float
    daily_limit_usd: float = 0.0
    monthly_limit_usd: float = 0.0

    @property
    def exhausted(self) -> bool:
        return self.day_pct >= 1.0 or self.month_pct >= 1.0


def period_starts(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(TZ)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


class BudgetTracker:
    def __init__(self, cfg: BudgetConfig, store: Store, prices: dict[str, Price]):
        self.cfg = cfg
        self.store = store
        self.prices = prices

    def price_for(self, model_id: str) -> Price:
        try:
            return self.prices[model_id]
        except KeyError:
            # Unknown model: refuse to run rather than spend untracked money.
            raise KeyError(f"no price configured for {model_id}; add it to pricing.yaml") from None

    async def override(self) -> dict | None:
        return await self.store.get_pref(OVERRIDE_OWNER, OVERRIDE_KEY) or None

    async def limits(self) -> tuple[float, float]:
        """(daily, monthly) in USD: the run-time override where set, else agent.yaml."""
        o = await self.override() or {}
        return float(o.get("daily_usd") or self.cfg.daily_usd), float(o.get("monthly_usd") or self.cfg.monthly_usd)

    async def set_override(self, daily_usd: float | None, monthly_usd: float | None, by: str, at: datetime) -> dict:
        """Change the limits now. None leaves that limit as it is; both None clears the override."""
        for v in (daily_usd, monthly_usd):
            if v is not None and not 0 < v <= 10_000:
                raise ValueError("a budget must be more than 0 and at most US$10,000")
        if daily_usd is None and monthly_usd is None:
            await self.store.set_pref(OVERRIDE_OWNER, OVERRIDE_KEY, None)
            return {}
        current = await self.override() or {}
        new = {"daily_usd": daily_usd if daily_usd is not None else current.get("daily_usd"),
               "monthly_usd": monthly_usd if monthly_usd is not None else current.get("monthly_usd"),
               "by": by, "at": at.isoformat()}
        new = {k: v for k, v in new.items() if v is not None}
        await self.store.set_pref(OVERRIDE_OWNER, OVERRIDE_KEY, new)
        return new

    async def state(self, now: datetime) -> BudgetState:
        day_start, month_start = period_starts(now)
        day = await self.store.spend_since(day_start)
        month = await self.store.spend_since(month_start)
        daily, monthly = await self.limits()
        return BudgetState(day, month, day / daily, month / monthly, daily, monthly)

    async def record(self, usage: Usage) -> Usage:
        # A provider that reports each call's cost (OpenRouter) is taken at its word when the model has no price
        # here; a model with neither is refused (price_for), as before.
        if not (usage.cost_usd and usage.model_id not in self.prices):
            usage.cost_usd = cost_usd(self.price_for(usage.model_id), usage)
        for key, value in USAGE_META.get().items():
            if getattr(usage, key, None) is None:
                setattr(usage, key, value)
        await self.store.record_usage(usage)
        return usage

    async def alerts_due(self, now: datetime) -> list[str]:
        """Alert messages not yet sent this period. Marks each as sent."""
        st = await self.state(now)
        local = now.astimezone(TZ)
        due: list[str] = []
        for period, pct, spent, limit, key in (
            ("daily", st.day_pct, st.day_usd, st.daily_limit_usd, local.strftime("%Y-%m-%d")),
            ("monthly", st.month_pct, st.month_usd, st.monthly_limit_usd, local.strftime("%Y-%m")),
        ):
            for threshold in (self.cfg.alert_at, 1.0):
                alert_key = f"{period}:{key}:{threshold:.2f}:{limit:g}"  # a raised limit alerts afresh
                if pct >= threshold and not await self.store.alert_sent(alert_key):
                    await self.store.mark_alert(alert_key)
                    word = "reached" if threshold >= 1.0 else f"passed {threshold:.0%} of"
                    due.append(f"{period} budget {word} US${limit:.2f} (spent US${spent:.2f})")
        return due
