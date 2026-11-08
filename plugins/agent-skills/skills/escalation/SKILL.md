---
name: escalation
description: "Chasing people who owe something (an answer, a review, a ticket update), warning them before it goes further, and escalating with the overdue follow-ups list by email and Teams when they still don't respond. Use whenever you follow up with someone, someone asks you to chase a person, or for the daily escalation sweep, e.g. 'chase Sam for the SLD', 'who's overdue', 'escalate the overdue items'."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Chasing and escalation

The point is to get the answer, not to shame anyone. The list exists so that the people who can unblock things see what's stuck. Be friendly in every chase, factual in every warning, and never editorialise about a person on the list.

## 1. Chasing

- **Always use `chase`**, never `send_teams_message` or `send_email`, for a follow-up. Otherwise it isn't counted and the rules below never apply.
- **One item, one name.** `item` is short and stable, e.g. "PRJ-371 site plan review". Use the same name for every chase about it, and give `link` (the ticket or document) the first time.
- **Channels:**
  - `teams` (first choice);
  - `email`;
  - `jira`: a comment on `jira_key` that @mentions them. Best when the work is on a ticket.
  - `phone`: a Teams call. Only from a chat with a person, never from a sweep.
- **Vary the channel.** A warning needs chases on more than one channel (`chase_status` gives the rules). Three Teams messages alone don't count.
- **Spacing.** A chase sooner than the configured gap is refused. Wait; don't switch to send_teams_message to get round it.
- **Each chase says:**
  - what's needed and by when;
  - why it matters (what it blocks);
  - the link;
  - how to answer, e.g. "reply here, or 'not mine' if it isn't".

## 2. Warning

- When `chase_status` shows `due_warning`, call `warn`. It sends the person:
  - every chase, with its date and channel;
  - the deadline;
  - who the overdue list goes to;
  - their record (times on the list before).
- Don't write your own warning, and don't threaten anything beyond what `warn` says.
- If they answer before the deadline, `close_chase` (answered). That's the outcome we want.

## 3. Escalating

1. **Check first.** For every item that is `due_escalation`, look at whether it's still really outstanding: the Jira ticket, their replies in Teams or email. Close anything that has been answered or done (`close_chase`). Never list someone who has replied, even with "I'm on it by Friday".
2. **Draft.** Call `escalate`. With approval on (the default), it returns a draft and its id. Give the draft to the approver as it stands.
3. **Send.** It goes out only when an approver asks you to send it (`send_escalation` with the id). The tool checks who is asking.
   - It's emailed to the configured list, copying the people on it, and posted in the escalation Teams chat.
4. **After sending:**
   - each listed item stays open;
   - its chase count starts again;
   - it needs a fresh warning before it can be listed again;
   - the person's record keeps every warning and escalation.

## 4. Daily sweep (weekdays, 09:00)

1. `chase_status`.
2. Close what's answered or done.
3. `warn` everyone who is due.
4. `escalate` what's due and report the draft to the approver.
5. Report in a few lines: warnings sent, items on the draft, items closed. Nothing due: one line.

## 5. Never

- Never chase or list anyone outside {org}, or anyone on the `exempt` list (they're still chased, never listed).
- Never add your own opinion of a person to a chase, warning or list. Dates, counts and links only.
- Never send the list without an approver's go-ahead when approval is on. "Looks fine" from someone who isn't an approver doesn't count.
