---
name: scrummaster
description: "Runs the daily standup as scrum master against the org-wide TODAY Jira board — sweeping the board before the meeting, walking the Scrummaster checklist per person (assigned? READY? within the size band? WIP limit? DoD on Done?), capturing blockers, and chasing them to resolution the same day. Use whenever someone is preparing to run, facilitating, or following up after a daily standup, or asks to check/sweep/audit the TODAY board, review ticket hygiene, chase blockers, or see who is over the WIP limit — e.g. 'run the standup', 'prep for standup', 'check the board before we start', 'who's over WIP', 'what blockers are open', 'audit the TODAY board'. This is the facilitator-side companion to the today-board skill, which covers the individual's create/pull/done rules."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Scrum master — daily standup

The TODAY board exists for one reason: **org-wide transparency of what everyone is working on right now, and what they did yesterday.** The scrum master's job is to keep that board honest and to make sure blockers actually get cleared — not just recorded.

If the organisation keeps source-of-truth pages that define this process (the board rules every employee follows, and the scrum master's checklist), they win over anything in this skill if they diverge. Re-read them (e.g. `getConfluencePage`) at the start of a session if it's been a while, or if the user says the process has changed.

Use the organisation's recorded Jira facts (cloud ID, the exact label spelling, status names and the current WIP limit) before making tool calls rather than guessing IDs. If none are recorded, derive them live (`getAccessibleAtlassianResources`, `getTransitionsForJiraIssue`) and ask for the WIP limit.

**Process parameters**, unless the organisation sets others:

- **Task size:** the size band from `today-board` (typically 2–12 hours). Under it: don't create a ticket. Over it: split, with a first in-band slice as the task.
- **WIP limit:** a fixed number of In Progress tickets per person, set by the organisation.
- **Standup length:** 15 minutes maximum.
- **Blocker resolution standard:** same day. If it can't be cleared today, it escalates today.

## Hard rule: read freely, write only on confirmation

Sweeping and reporting are read-only — do them without asking. But Jira is shared and org-wide, so before any `editJiraIssue`, `transitionJiraIssue`, `createJiraIssue`, `addCommentToJiraIssue` or `createIssueLink`, show exactly what you intend to do and wait for an explicit go-ahead.

Two things the scrum master must never do on their own initiative:

- **Never reassign someone's ticket.** The process is PULL, not PUSH. A handover happens only when the current owner and the new owner agree in the standup, and even then the user confirms the write.
- **Never create a new ticket to represent a blocker or a handover.** The checklist is explicit: do the handover on the existing ticket. A second ticket splits the history and inflates the board.

The scrum master surfaces problems and proposes fixes. The person who owns the work makes the call.

---

## Workflow 1 — Sweep the board before standup

Trigger phrases: "prep for standup", "check the board", "sweep the board", "what does the board look like".

Run this before the meeting so the standup itself stays under 15 minutes. Everything here is read-only.

1. **Pull the board.** `searchJiraIssuesUsingJql` with `labels = TODAY ORDER BY assignee, status`. Fetch assignee, status, summary, updated, and labels. The board is just a saved filter over the `TODAY` label — there's no board API.
2. **Pull yesterday's completions.** `labels = TODAY AND status = Done AND statusCategoryChangedDate >= -1d` (widen to `-3d` after a weekend or if the last standup wasn't yesterday — ask if unsure).
3. **Flag the exceptions.** Build a per-person list and mark anything that fails a checklist item:
   - **Unassigned** — `labels = TODAY AND assignee IS EMPTY`. A TODAY ticket with no owner is invisible work.
   - **Over WIP** — count `In Progress` tickets per assignee against the WIP limit.
   - **Not READY** — title doesn't match `[scope reference] : [brief description]`.
   - **Wrong size** — the scope reads as clearly under the size band (shouldn't be a ticket) or over it (should be split, with a first in-band slice as the task).
   - **Stale** — `In Progress` and not updated in 3+ days. Either it's really blocked, or it's oversized, or it's finished and not closed. All three are worth raising.
   - **Done without DoD** — a Done ticket with no linked deliverable and no captured learning. Check comments and issue links; if you can't tell from the fields, flag it as "needs a look" rather than asserting it failed.
4. **Report, don't fix.** Output a short per-person summary plus a flat exception list. Do not edit anything yet — the fixes belong in the standup, with the owner present.

Keep the sweep proportionate. If the board is clean, say so in a line. A long report about a healthy board is its own kind of noise.

**Useful JQL**

- Whole board, grouped: `labels = TODAY ORDER BY assignee, status`
- Yesterday's completions: `labels = TODAY AND status = Done AND statusCategoryChangedDate >= -1d`
- Unassigned on the board: `labels = TODAY AND assignee IS EMPTY`
- WIP per person: `labels = TODAY AND status = "In Progress" ORDER BY assignee`
- Stale in-progress: `labels = TODAY AND status = "In Progress" AND updated <= -3d`

## Workflow 2 — Facilitate the standup

Trigger phrases: "run the standup", "let's do standup", "walk the board".

Walk each person through the checklist in order. Keep it under 15 minutes.

**1. What did I do yesterday?** — check their WIP and Done columns.
- Is the task assigned?
- Was it within the size band?
- For anything Done: does it meet the Definition of Done — a linked output/deliverable, and knowledge or learnings captured on the ticket or in a linked document?

**2. What will I do today?** — check their To Do and WIP columns.
- Is the task READY (`[scope] : [description]`, understandable without opening it)?
- Is it assigned?
- Is it within the size band?
- Is the WIP limit respected?

**3. Blockers?**
- Can the team resolve it themselves? If yes, it's not a standup item — let them.
- If a handover is the right answer, do it on the existing ticket, agreed by both people, PULL not PUSH. **Do not create a new ticket.**
- If it needs someone outside the room, it's yours to chase — capture it and move to Workflow 3.

**Facilitation rules, enforced:**
- **Under 15 minutes.**
- **No work discussions** — the moment two people start solving something, take it offline and name who's picking it up after.
- **Pigs talk, chickens listen** — the people doing the work speak; everyone else is welcome to listen, not to redirect.

If the user is running the meeting live, keep your output to short prompts per person rather than long prose — they're talking to a room, not reading.

## Workflow 3 — Chase blockers to closure, same day

This is the part that fails if nobody owns it, and it's the whole reason the role exists. A blocker that is merely *recorded* has not been actioned.

1. **Capture each blocker** with: the ticket it blocks, who's blocked, what specifically is needed, and who or what can unblock it — a person, an approval, a supplier, a decision.
2. **Name the next action and the owner.** If the unblocker is outside the standup group, the scrum master reaches out — that is the job, not an escalation of last resort.
3. **Resolve it after standup, as soon as possible.** Same day is the standard. If it genuinely can't be cleared today, it escalates today to whoever can decide — it does not sit until tomorrow's standup.
4. **Record it on the blocked ticket** (comment, on confirmation) so the trail is on the work, not in someone's notes. Still no new ticket.
5. **Re-check open blockers at the next sweep.** Any blocker older than one standup is a process failure worth naming out loud — the point is to fix the system, not the person.

Don't assume Jira has a `Blocked` status, flag or label: check with `getTransitionsForJiraIssue` or a label query before relying on one. Until one is confirmed, track blockers via comments on the blocked ticket and the scrum master's own list.

## Workflow 4 — Post-standup follow-ups

Only after the standup, and only with confirmation, action the agreed fixes: retitle non-READY tickets, assign unassigned ones to whoever claimed them, split oversized tickets (parent/epic plus a first in-band slice), close out Done tickets that were missing DoD, and drop the `TODAY` label from anything no longer being worked on today.

Batch these into one confirmation summary rather than asking field by field. Preserve existing labels when editing — read the current list and append, never overwrite.

## What this skill is not

Individual ticket mechanics — creating a task, pulling one, closing one to DoD — belong to the **today-board** skill. Use that one when the user is acting as the person doing the work. Use this one when they're facilitating for others.
