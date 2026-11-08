---
name: today-board
description: "Implements the \"TODAY board\" Jira ways-of-working — creating a properly-sized, READY-titled task, pulling/starting work on one (status + TODAY label), and closing one out (Definition of Done: link the deliverable, capture learnings). Use this whenever someone wants to log what they're working on today, add or start a Jira task the organisation's way, pull or hand over a ticket, or mark a ticket done — even if they don't say \"TODAY board\" by name, e.g. \"let's turn this into a task\", \"I'm starting on PRJ-123 now\", \"can you hand this ticket to Sam\", \"mark this done and note what I learned\". Always check this skill before creating, transitioning, or closing a Jira issue for the organisation's work, since the process has specific rules (title format, task sizing, pull-not-push, DoD) that a generic Jira create/edit would miss."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# TODAY board workflow

The TODAY board exists for one reason: **org-wide transparency of what everyone is working on right now, and what they did yesterday.** Every rule below exists to keep that board honest and useful — a title you can understand without opening the ticket, a size that means "still relevant today," an assignee that reflects who actually owns the work, and a paper trail when it's done. Apply the *intent*, not just the letter, when a situation doesn't fit neatly.

The board itself is just a saved filter over the `TODAY` label — there's no special board API. Everything here is done with ordinary Jira issue operations.

**Size band.** A task must be big enough to be worth tracking and small enough to finish while it is still relevant. The organisation sets the band; a typical one is **2–12 hours**, and that is used below unless the organisation's rules give another.

## Hard rule: always confirm before writing

Jira is shared, org-wide, and visible to the whole company — a wrong title, a silent reassignment, or a premature "Done" is not private scratch work. Before calling **any** of `createJiraIssue`, `editJiraIssue`, `transitionJiraIssue`, `addCommentToJiraIssue`, or `createIssueLink`, show the user exactly what you're about to do (title, project, assignee, status, label, comment text) and wait for an explicit go-ahead in that turn. Batch the confirmation for a single workflow into one clear summary rather than one confirmation per field — but never skip it.

This matters most for assignment: the process is **PULL, not PUSH**. Assigning a ticket to someone other than the requesting user is only ever a deliberate, named handover ("assign this to Sam") — never a default, never something you infer because a topic "belongs" to someone else's area. If in doubt, ask who it should go to rather than guessing.

## Reference facts for this org

Before making tool calls, use the organisation's recorded Jira facts (Atlassian cloud ID or site, the exact label spelling, the status names) so you're not guessing IDs. If any of those facts stop matching reality (a query 404s, a transition name isn't found), or none are recorded, derive them live with `getVisibleJiraProjects` / `getTransitionsForJiraIssue` rather than forcing a stale value.

---

## Workflow 1 — Add a new task

Trigger phrases: "create a task for X", "add this to Jira", "log a task", "I need to track this work".

1. **Find the space.** Ask which Jira project this belongs to unless it's obvious (e.g. the user names a project key, or references a parent/epic already in a known project). Don't guess a project silently — wrong-project tickets are invisible to the right team.
2. **Find the scope reference.** Ask what larger piece of work this sits under — an epic, an initiative name, a parent ticket key, or a one-or-two-word programme name. This becomes the `[reference to larger scope of work]` half of the title. If there genuinely isn't a bigger scope (rare — most work ladders up to something), ask the user to confirm that before proceeding, don't just invent one.
3. **Check for near-duplicates.** Before drafting anything, search Jira (`searchJiraIssuesUsingJql`, e.g. matching keywords from the scope reference and task description, optionally scoped to the target project) for existing open tickets that might already cover this work. A duplicate, uncoordinated ticket is exactly the kind of board noise this process exists to prevent — two people can easily end up tracking the same piece of work without knowing it. If you find plausible overlap, surface it to the user (ticket keys, titles, current assignee) before proceeding, and ask whether this is genuinely separate work or should instead become a comment/subtask on the existing ticket. Don't skip this even if the user seems confident it's new — the search costs one tool call and prevents real coordination problems. If nothing relevant turns up, just say so briefly and move on — no need to force a discussion when the search comes back clean.
4. **Size it.** Ask or estimate whether the work is doable in **2–12 hours** (the size band).
   - Under 2h: don't create a ticket. Tell the user to just do it, or fold it into an existing/parent ticket instead. Creating a ticket for a 20-minute task is exactly the noise this process is trying to avoid.
   - Over 12h: it's too big for a single TODAY-board task. Suggest breaking it into an epic/parent with a first 2–12h slice as the actual task, and say so explicitly rather than creating one oversized ticket.
5. **Title it READY.** Format: `[scope reference] : [brief description of the task]`. The colon-separated format is what lets someone scanning the board understand a ticket without opening it — don't compress it into a single free-form sentence.
6. **Assign it.** Default to the requesting user (PULL). Only assign elsewhere if the user explicitly names a person as a deliberate handover (see hard rule above).
7. **Confirm, then create.** Show the full draft — project, title, assignee, description — and only call `createJiraIssue` after the user confirms.

Note the ticket does **not** get the `TODAY` label at creation. The label is what puts a ticket on the board, and per the process that only happens when someone actually starts working on it *today* (Workflow 2) — a ticket can sit READY and unstarted without cluttering the board. Don't add the label here even if it seems convenient to do it all at once.

## Workflow 2 — Start work on a task

Trigger phrases: "I'm starting on TICKET-123", "pull this ticket", "let's begin X today", "put this on the board".

1. **Fetch the issue** (`getJiraIssue`) to see its current assignee, status, title, and labels.
2. **Check assignment (PULL).** If it's unassigned or already assigned to the requesting user, self-assign as needed. If it's assigned to a *different* named person, don't silently reassign it — confirm with the user that this is an intentional pull of someone else's ticket before changing it, since that's a change visible to that other person.
3. **Re-check sizing.** If the ticket's scope reads as clearly under 2h or over 12h, flag it rather than mechanically starting it — e.g. "this looks like a 30-minute fix, the process says just do it rather than tracking it — start it anyway, or handle it differently?"
4. **Re-check the READY title.** If it doesn't match `[scope] : [description]`, propose a corrected title and get confirmation before renaming it — don't rename silently, the original title may have been intentional.
5. **Transition to "In Progress."**
6. **Add the `TODAY` label**, preserving any existing labels (read the current label list first, append rather than overwrite).
7. **Confirm, then execute.** Summarize the intended changes (assignee if changing, status → In Progress, + TODAY label) once and get a go-ahead before making the calls.

## Workflow 3 — Done

Trigger phrases: "I finished X", "mark TICKET-123 done", "close this out", "wrap up today's ticket".

The Definition of Done has two parts and both are required — don't transition to Done on request alone without them:

1. **Link the output.** Ask for (or use, if already given) a link to whatever was actually produced — a PR, a document, a dashboard, an artifact, another Jira/Confluence item. Record it on the ticket via a comment (`addCommentToJiraIssue`) or a proper issue link (`createIssueLink`) if the deliverable is itself a trackable item.
2. **Capture the learning.** Ask what's worth remembering from doing this — a decision made, a gotcha hit, a number discovered, anything that would help someone else (or future-you) who runs into this again. One sentence is enough. If there's genuinely nothing to note, get the user to confirm that explicitly rather than skipping the question — the point is a deliberate check, not a rubber stamp.
3. **Confirm the comment text and the transition**, then post the comment/link and transition the issue to "Done".

If the user asks to mark something Done without a deliverable link or without having thought about learnings, don't just comply — ask for the missing piece first. That's the entire point of this step; skipping it defeats the purpose of the board as an organizational record, not just a task tracker.
