"""Prometheus metrics."""

from prometheus_client import Counter, Gauge, Histogram

# channel and trigger are small fixed sets (agent.channel_of); people and sessions are never labels (unbounded):
# they are in the model_usage table, which Grafana reads with SQL.
TURNS = Counter("agent_turns_total", "Conversation turns handled", ["agent", "tier", "outcome", "channel", "trigger"])
TOKENS = Counter("agent_tokens_total", "Model tokens", ["agent", "tier", "model", "kind", "channel", "trigger"])
COST = Counter("agent_cost_usd_total", "Model spend in USD", ["agent", "tier", "model", "channel", "trigger"])
ESCALATIONS = Counter("agent_escalations_total", "default->deep escalations", ["agent", "reason"])
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls", ["agent", "tool", "status"])
TURN_LATENCY = Histogram("agent_turn_seconds", "End-to-end turn latency", ["agent", "tier"],
                         buckets=(0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300))
KILLSWITCH = Gauge("agent_killswitch_engaged", "1 when outbound actions are stopped", ["agent"])
BUDGET_PCT = Gauge("agent_budget_used_ratio", "Share of budget used", ["agent", "period"])
