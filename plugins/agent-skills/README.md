# agent-skills

Organisation-neutral skills for AI team agents and the people working with them: Office documents, document and
pull-request review, Jira (posting, timelines, sprint boards, the daily board), construction management and
contracts, project budgets and cost control, project accounting, chasing and escalation, and figures.

Kept in its own folder so it can become its own repository; agent-core packages it (`agentcore/builtin_skills`).

## How agents use it

agent-core's skill loader looks in the deployment's skills first and then here. A deployment adds its own rules
to a skill here with a skill of the same name whose front matter says `extends: core`:

```markdown
---
name: office-documents
description: "..."
version: 1.0.0
owner: Acme
applies_to: [ada]
extends: core
---

## Acme rules

Number documents PRJ-<discipline>-<type>-nnnn; ask the document controller for the number.
```

The agent then gets this skill's text after the core one, as one skill, with both skills' references.

`{org}`, `{place}`, `{admin}` and the other placeholders are filled from the deployment's `org.yaml`. Read them as
"your organisation", "your local time", "whoever administers the agents" when you use these skills directly.

## As a Claude plugin

`.claude-plugin/plugin.json` makes the folder a Claude plugin, so people can install the same skills in Claude
Code or Cowork.

## Licence

MIT (`LICENSE`).
