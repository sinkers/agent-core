---
name: post-to-jira
description: "How to write anything into Jira — author the body in ADF (not markdown) so checklists render, link documents (SharePoint or the document library) rather than paths, and link tickets rather than bare keys. Use for any Jira create, edit or comment."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Posting to Jira

This covers **how a Jira body is written**. It does not cover the organisation's process rules — title format, task sizing, pull-not-push, the TODAY label, Definition of Done. Those live in the `today-board` skill, and both apply: check `today-board` for whether and how to create or transition the ticket, then this skill for what the body looks like.

A Jira ticket is read by people who do not have your shell, your session or your folder tree — often on a phone, often in a notification email. Everything below follows from that.

## 1. Author the body in ADF, not markdown

Pass `contentFormat: "adf"` and give `description` (or `body`) as an ADF document object:

```json
{ "type": "doc", "version": 1, "content": [ ...block nodes... ] }
```

**Jira is not markdown.** Only the forms the Jira tools convert (typically tables, panels, headings, lists, code, rules, quotes) render; anything else posts as literal characters.

The MCP tools accept markdown and convert it, and for a short prose-only comment that is fine. But the converter escapes anything it does not recognise, and **checkboxes are the case that silently breaks** (see §2). Default to ADF for any ticket body with structure.

The node vocabulary you will actually use:

| Need | ADF |
|---|---|
| Paragraph | `{"type":"paragraph","content":[ ...inline... ]}` |
| Heading | `{"type":"heading","attrs":{"level":2},"content":[...]}` |
| Bold / italic | text node with `"marks":[{"type":"strong"}]` or `{"type":"em"}` |
| Link | text node with `"marks":[{"type":"link","attrs":{"href":"https://..."}}]` |
| Bullet list | `bulletList` → `listItem` → `paragraph` |
| Checklist | `taskList` → `taskItem` (see §2) |
| Table | `table` → `tableRow` → `tableHeader` / `tableCell` → `paragraph` |
| Horizontal rule | `{"type":"rule"}` |
| Quoted source wording | `blockquote` → `paragraph` |

Two things that trip people up: a **link is a mark on a text node**, not a node of its own; and `listItem` wraps its text in a `paragraph` while `taskItem` does **not** (§2).

## 2. Checklists — the one that silently fails

Markdown `- [ ] Item` does **not** produce a Jira checklist. The converter escapes it to `\[ \]` and it renders as literal bracket characters. Nobody can tick it.

Use `taskList` / `taskItem`:

```json
{"type":"taskList","attrs":{"localId":"deliverables"},"content":[
  {"type":"taskItem","attrs":{"localId":"d-1","state":"TODO"},
   "content":[{"type":"text","text":"Transformers"}]},
  {"type":"taskItem","attrs":{"localId":"d-2","state":"DONE"},
   "content":[{"type":"text","text":"Dry coolers"}]}
]}
```

- `state` is `"TODO"` or `"DONE"`.
- Every `localId` must be unique within the document — both on the list and on each item. Use meaningful slugs (`d-1`, `i-sb02`) so a later edit can keep tick state aligned.
- `taskItem` content is **inline nodes directly** — text nodes with their marks. Do not wrap them in a `paragraph`; that is the `listItem` pattern.
- Inline links work inside a `taskItem`, so an item can point at the ticket that delivers it.
- Group long checklists into several `taskList`s with a `paragraph` heading above each, rather than one 19-item list.

**How to check it worked.** The tools return markdown on read whatever `responseContentFormat` you ask for. Real task items come back as `- [ ]`; escaped literal text comes back as `\[ \]`. If you see backslashes, the checklist did not take.

## 3. Documents get a link, never a path

```
[Human-readable document name](https://example.sharepoint.com/...)
```

The locator goes *after* the link — sheet, section, row, page:

> — [Site Information Request, working v2](https://…), sheet *Information Request*, row SB-02.

Not `Site_Information_Request_20260915_working_v2.xlsx` in `2 Sites / 2.4 Projects / …`. Link text is the document's name as a person would say it, not the filename with underscores; keep the revision in it when it matters ("Load Schedule Rev Q"). Never use a raw URL as link text.

**Where to get the URL, in order of preference:**

1. **A sharing link the user pasted** (`https://<tenant>.sharepoint.com/:x:/s/<site>/<token>?e=…`). Best — it survives moves and renames and carries the permission grant. Keep the whole thing including `?e=`; that is part of the share token, not tracking to strip.
2. **The document search tool** → `webUrl` per hit. Percent-encode before pasting: space → `%20`, `&` → `%26`, `(` → `%28`, `)` → `%29`. An unencoded space breaks the link at that point.
3. **`sharepoint_get`** → use `webUrl`, never `download_url`. That one is pre-authenticated and expires in about an hour, so it is dead by the time anyone reads the ticket.

## 4. Tickets get a browse link, never a bare key

Write `[PRJ-161](https://example.atlassian.net/browse/PRJ-161)`, not `PRJ-161`. Jira auto-links keys in some views but not in notification emails — do not rely on it. Key as the link text, short description after:

> — [PRJ-161](https://example.atlassian.net/browse/PRJ-161) — Finalise site layout for Concept A. Delivers SB-02. In Progress.

The pattern is always `<Jira site URL>/browse/<KEY>`.

## 5. Known traps

**Straight double quotes in folder names break the Graph path API.** A folder like `Client "Indigo"` makes `sharepoint_get` / `sharepoint_put` fail with `400 invalid character '"'`. Do not try to escape it — use the document search tool to get the `webUrl` instead of constructing a path.

**The search index goes stale on renames.** A `webUrl` from search can name a folder that has since been renamed, and `folder_search` may disagree. When the two disagree, say so or ask — do not publish a link you have not resolved.

**Never invent a URL from a path.** Building `https://<tenant>.sharepoint.com/sites/<site>/Shared Documents/<path you believe in>` produces a plausible-looking dead link, which is worse than no link. No tool result and no user message gave you a URL? Then you do not have one.

**Link the file you actually quoted.** Where a document exists as `.md` and `.xlsx`/`.pdf` companions, quote from one and link that one; if you only hold the companion's URL, say which file the wording came from.

**`editJiraIssue` replaces the whole description.** There is no partial edit. Read the current body first and send it back complete with your change applied.

## 6. When a document has no home yet

A deliverable that exists only in the chat session or the cloud workspace has no link. Two honest options:

- **File it first, then link it.** Documents belong in the document library (SharePoint/OneDrive) rather than as chat artifacts, so this is usually right.
- **Say plainly it is not filed:** "working paper prepared, awaiting a SharePoint home — link to be added once filed", and ask where it belongs.

Never leave a local path (`/mnt/user-data/outputs/…`, `~/Library/CloudStorage/…`) or a bare filename sitting in a ticket.

## 7. Before you finish

Read back the body you just wrote and check:

- every document mention is a link, or is explicitly flagged as not yet filed;
- every ticket mention is a browse link;
- every checklist came back as `- [ ]`, not `\[ \]`;
- no local paths, no bare filenames, no naked issue keys.

Fix anything that fails before moving on.
