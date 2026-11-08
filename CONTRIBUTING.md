# Contributing

Issues and pull requests are welcome. This is maintained by one person, so please open an issue before a large change.

```bash
uv sync --extra test
uv run pytest
```

- Branch from `main` and use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`).
- Add a test with every behaviour change.
- Keep the engine neutral: nothing specific to one organisation. Names, domains, holidays and branding belong in a
  deployment's `org.yaml`, `agent.yaml` and skills.
- Never commit secrets. Tests use made-up values on `example` domains.

CI runs on the maintainer's own runners, so a first-time contributor's pull request waits for approval, and it may be
tested on a branch here instead. Changes to `.github/` get extra scrutiny.
