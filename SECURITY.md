# Security

## Reporting a vulnerability

Please use GitHub's private reporting (the repository's **Security** tab, **Report a vulnerability**), not a public
issue. This is maintained by one person alongside other work, so replies are on a best-effort basis.

## What the engine does

Every tool call goes through one gateway that applies the agent's allow-list, confirmations, site and path rules and
budgets, and logs it. `docs/design/agent-core.md` section 7 covers the trust boundaries.

## Known gaps

Read these before running an agent that holds real credentials or takes input from people you don't trust.

| Gap | What to do meanwhile |
|---|---|
| Hindsight has no auth in the always-on charts, so any agent pod could read or delete any memory bank | One Hindsight per trust boundary, or the memory-proxy from `deploy/scale-to-zero-poc/memory/` |
| NetworkPolicies in the charts are ingress-only, with no egress rules and none for Postgres | Add default-deny egress per namespace, and allow Postgres from the agents only |
| Budgets are enforced inside the agent process, so a compromised worker could skip recording | Set a hard spend limit at the model provider too |
| The wake-on-demand front door has shared client and admin tokens | Treat either token as full access, and keep the front door off the public internet |
| `/metrics` has no auth | Restrict it to your monitoring namespace with a NetworkPolicy |
| The wake-on-demand API trusts its one client token: every client shares the `api:` ids and can read each other's API results | Give the token only to one trusted service |
| Wake-on-demand workers can call any Bedrock model their policy allows, and report their own usage | Limit the IAM policy to the models you want |
| `deploy/scale-to-zero-poc/` is a trial on a private network, not a production setup: its image registry has no auth and is writable by anything that can reach a node, its voice-gateway has no auth, the `agents`, `platform` and `voxhelm` namespaces have no NetworkPolicies, and a failed delivery isn't retried | Don't run it as it stands for real users or on a shared network. Its test hooks can crash workers, so only set `WORKER_TEST_HOOKS=1` for test runs |
