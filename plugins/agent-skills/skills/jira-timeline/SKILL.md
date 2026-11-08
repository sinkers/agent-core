---
name: jira-timeline
description: "How to build, fix or check a project timeline in Jira so it reads correctly: start and due dates on every bar, epics as stages, Blocks links for dependencies (pointing the right way), rank so the timeline reads in order, releases as milestones, and keeping it in step with the project plan (Gantt / MS Project). Use whenever someone asks to put a plan into Jira, lay out or tidy a Jira timeline or roadmap, set dates, link dependencies, reorder tickets, or asks why the timeline looks wrong — e.g. 'get PRJ onto the Jira timeline', 'link these so they show as dependencies', 'why is that arrow red', 'make Jira match the Gantt'."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Building a Jira timeline

The Jira timeline is a picture Jira draws from a handful of fields. It shows exactly what is in those fields and nothing else: a ticket with no Start date has no bar start, a "Relates" link draws no arrow, and a ticket ranked out of order sits in the wrong row. Almost every "the timeline is wrong" question is one of the fields below. Set them deliberately, then check with `timeline_audit`.

Write tickets the organisation's way as well (`today-board` for titles, sizing and Definition of Done; `post-to-jira` for bodies and links). This skill covers only what makes the timeline right.

## 1. What the timeline draws

| You see | Jira reads | Set it with |
|---|---|---|
| A bar's start and end | the system **Start date** field and **Due date** (`duedate`), and nothing else | `update_issue(key, start_date=, due_date=)` (`YYYY-MM-DD`; `none` clears) |
| A faded, open-ended bar | only one of the two dates is set | set both |
| An epic's bar | its own dates if set; if both are empty, rolled up from its children (striped bar) | see §3 |
| Rows and their order | the hierarchy (epic, then its children) and **Rank** | `update_issue(parent=EPIC)`, `rank_issues` |
| Arrows | **Blocks** links only, within the same project | `link_issues(blocker, blocked)` |
| A red arrow | the blocked ticket starts on or before the day its blocker is due | move the dates; never delete the link to hide it |
| Release markers (milestones) | releases (versions) with a release date, when **Releases** is ticked in View settings | `update_issue(fix_versions_add=[...])` |
| Progress on an epic | the status category of its children (same project only) | statuses, not dates |

What it never shows: subtasks; levels above Epic (those need Jira Premium Plans); other projects' tickets; custom date fields such as "Target start" (Plans only); and more than 5,000 items or 500 epics.

Children's bars only appear once **child-level scheduling** is on (timeline or board settings). On a Scrum board it can be "by sprints" or "by dates". **By sprints** means a child's bar is its sprint and its own dates are ignored. Pick one mode per board. Projects run to a plan (construction, engineering) normally use **by dates**.

## 2. Before you start

1. **Check permissions** once per project: `jira_get("/mypermissions", {"projectKey": "PRJ", "permissions": "SCHEDULE_ISSUES,EDIT_ISSUES,LINK_ISSUES"})`.
   - **Schedule Issues** is what lets you change due dates and rank. Edit Issues alone is not enough.
   - If one is missing, say which, and ask a Jira admin to add your Jira account to the project role that has it.
2. **Check the Start date field** is on the project's screens. If `update_issue` fails with "cannot be set... not on the appropriate screen", a company-managed project needs an admin to add Start date to its screens and field configuration. Team-managed projects have it already.
   - The tools find Jira's own (locked) Start date even when someone has made a second field with the same name. If you set it by name through `fields`, you may hit the duplicate. Use `start_date`.
3. **Ranking needs the board** to rank. A company-managed board's filter must end in `ORDER BY Rank ASC`; if it doesn't, the timeline sorts by the filter and nothing can be reordered.
   - If `rank_issues` returns 401 "scope does not match", your Jira credential lacks the Jira Software scope `write:issue:jira-software`. Tell {admin}; it isn't a Jira permission problem.

## 3. Lay it out

**Epics are stages or work packages, weeks to a few months long.** Children are deliverables or activities, days to about two weeks long, each one a ticket a person can finish. Don't put subtasks on a plan: they never show, so make them children instead.

- **Name for the row.** Lead with the plan's WBS code or stage so the timeline reads like the Gantt: `2.3 Remove PDUs and cabling (top deck)`. Keep within the today-board title rules.
- **Every child has a parent** (`update_issue(key, parent="PRJ-100")`). A ticket with no parent floats at the bottom with no stage.
- **Every child has both dates.** Due date is the **last working day** of the work, not the day after. When converting from MS Project or the plan tool, use the local calendar date. Never send a time or a time zone.
- **Epic dates: pick one approach per project.**
  - **Set them** (recommended when a plan is the master). The epic shows the approved stage window, and Jira warns when a child is scheduled outside it.
  - **Leave both empty** so the epic rolls up from its children.
  - Never set just one.
  - Setting an epic's dates by hand hides child slippage, so audit (§6) to catch children outside it.
- **Milestones.** Jira has no milestone object, so use one of two approaches:
  - **Prefer a release** for each stage gate, approval or handover, named for the milestone with its release date (for example `M2 - Top deck clear`). Add it to the fixVersions of the tickets that deliver it. Creating releases needs project admin, which an agent's account usually doesn't have. List them with `jira_get("/project/PRJ/versions")` and ask the project lead to create any that are missing.
  - **Fallback:** a one-day ticket (start = due) named `◆ M2 - Top deck clear`, a child of the stage epic. It shows as a short bar, not a diamond.
- **Owners.** Every open child has an assignee. Epics have the stage lead.

## 4. Dependencies

- **Use `link_issues(blocker, blocked)`** for every real finish-to-start dependency: "B can't start until A is done" is A blocks B. Only the **Blocks** link type draws an arrow. "Relates to", "Duplicates" and "Depends" draw nothing on the timeline.
- **Direction is the classic mistake.** The REST API and Atlassian's own documents disagree on which side of a new link means "blocks", and tools that guess get it backwards. `link_issues` reads the link back from the blocker and fixes it if Jira stored it reversed. Never create Blocks links any other way, and never with the Rovo `createIssueLink` tool.
  - To check a link by hand: on the blocker, `get_issue` shows the blocked ticket under links as an **outward** "blocks"; on the blocked ticket it shows as an **inward** "is blocked by".
- **Link at the level the dependency really is.**
  - Epic to epic for "Stage 2 can't start until Stage 1 is clear".
  - Ticket to ticket for "drain before cutting pipework".
  - Don't link every child of one epic to every child of the next.
- **Start-to-start, finish-to-finish and lags don't exist in Jira.** Represent them by the dates, and say so in the ticket (for example "can start 3 days after PRJ-12 starts").
  - Don't add a Blocks link for a start-to-start dependency: it would draw red wherever the work overlaps.
  - `plan_to_jira` lists those links under `not_drawn` rather than linking them.
- **A red arrow is a real schedule conflict.** Fix it by moving the blocked ticket's dates, or by challenging whether it really is blocked. Deleting the link only hides it.
- **No loops.** `link_issues` refuses to make A block B when B already blocks A.

## 5. Order (rank)

The timeline lists epics in rank order, and children in rank order within each epic. It must read top to bottom in the order the work happens.

- Call `rank_issues([keys in order])`. With no anchor, the first key stays where it is and the rest line up after it. Use `after=` or `before=` to place a group next to another ticket.
- Rank epics among epics, and each epic's children among themselves.
- Re-rank after adding tickets. New tickets go to the bottom.
- Ranking changes the backlog and board order too, so tell the team when you reorder a live board.

## 6. Check it: `timeline_audit`

Run `timeline_audit(project=)` or `timeline_audit(epic=)` after any build or change, and before telling anyone the timeline is ready. It lists the timeline in rank order and finds:

- **errors:**
  - a red dependency (the blocked ticket starts before its blocker is due)
  - due before start
- **warnings:**
  - no dates, or only one date
  - a child starting before or ending after its epic
  - overdue open tickets
  - too many items
- **info:**
  - subtasks (never shown)
  - an epic with no children
  - a ticket with no parent
  - a ticket with no owner
  - rank out of date order

Fix errors and warnings first. Report the rest as a short list with links (post-to-jira §4), not bare keys.

For the JQL behind these checks, the exact REST payloads, and the less common cases, see `references/jira-timeline-reference.md`.

## 7. Plan and Jira together

When a project has a plan (`make_plan`, an MS Project file or the Gantt viewer), **the plan is the master for dates, dependencies and order, and Jira is the master for status, assignee and progress.** Never edit dates in both, or they drift.

1. Put the Jira key on each task in the plan spec (`"jira": "PRJ-364"`), and on each stage summary task for its epic.
2. Run `plan_to_jira(plan_id)`. It previews:
   - Start and Due dates from the levelled schedule
   - each ticket's parent set to its stage's epic
   - a Blocks link for each finish-to-start dependency
   - the rank in plan order

   It compares all of this with Jira's current values, and lists tickets the plan names that Jira doesn't have.
3. Show the person the preview in a few lines: how many tickets change, the biggest date moves, and any missing tickets.
4. On their yes, run `plan_to_jira(plan_id, apply=true)`. It makes the changes and audits the result. Report the result and any failures.
5. When dates change in the plan, run it again. When someone moves a bar in Jira instead, update the plan (`make_plan` with the same plan_id) and re-run. Don't patch Jira by hand.
6. Tickets with no key in the plan aren't touched. Create them first (`create_issue` with `start_date`, `due_date` and `parent`, titled per today-board), then add their keys to the plan.

## 8. Don'ts

- **Don't schedule with sprints and dates on the same board.** Sprint scheduling silently overrides the dates.
- **Don't rely on custom date fields** ("Target start", "Planned finish"). The basic timeline ignores them.
- **Don't mix tickets from several projects** on one timeline. It shows one project only; cross-project plans need Jira Premium Plans.
- **Don't change a live timeline in bulk without saying so.** Preview first (§7), and tell the project lead what moved.
- **Don't promise the timeline looks right until `timeline_audit` is clean** for errors and warnings.
