---
name: office-documents
description: "Creating and editing Word, Excel and PowerPoint files in the organisation's house style: reports, briefs, plans, minutes, procedures, registers, budgets, trackers and decks with files__create_file; changing anyone's existing document (wording, sections, tables, cells, slides) with files__edit_file, as Word tracked changes and comments when it's someone else's; checking what a document holds (file_outline) and how it looks (preview_file). Use whenever you write or change a .docx, .xlsx or .pptx, or someone asks for a document, spreadsheet, register, tracker, deck, slides or a markup."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Word, Excel and PowerPoint

Documents, spreadsheets and decks are a project manager's main output. Make them look like the organisation made
them, check them by eye before they go out, and change other people's files the way a careful colleague would:
visibly.

## The house style is automatic

Everything is built on the brand pack's templates (Word, letterhead, Excel and deck), so files match what staff
produce.
- **Word** starts from the Word template: its styles and theme (heading and body fonts), the wordmark on the first
  page, the template's table style, and a footer with the title, the classification and the page number. On top go
  the type label, title, subtitle and title block from your front matter.
- **Letters** (front matter `type: Letter`) go on the letterhead (section below).
- **Excel** starts from the Excel template (its theme): title, body and number fonts, a coloured header row, rules,
  frozen header, filters, sized columns, real numbers and dates. Any guidance tab in the template never goes out.
- **PowerPoint** is built on the master deck: title, section dividers, comparison, table, three numbers, process,
  timeline, charts and closing layouts.

Without a brand pack a neutral built-in style applies. Don't restyle by hand. If someone needs a plain document for
an outside template, start the Markdown with `---` / `style: plain` / `---`.

## Word (.docx)

Start with front matter so the cover and title block are right:

```
---
type: Project report                 the label above the title: Project report, Working notes, Minutes,
                                     Engineering procedure, Brief, Options paper…
title: PRJ-1 long-lead equipment
subtitle: Concept phase · transformers and generators
number: PRJ1-GEN-RPT-nnnn            from the organisation's document numbering: ask whoever allocates numbers;
                                     until then keep nnnn. Never invent one. If there is a register, check a
                                     number there before you use it
revision: A
date: 14 October 2026
status: Draft for review
author: <your name> (AI) for Sam Taylor
reviewers: J. Lee (check), S. Taylor (approve)
scope: Long-lead equipment only. Construction packages are in the master RFQ list.
---
```

`status` is a sentence saying what the reader may rely on ("Working engineering assessment. Not an approved
design."). `revision` can say what changed ("B: transformer lead times from the supplier's proposal"). Every
engineering document follows the organisation's engineering writing style: topic headings, facts stated and
stopped, third person, a source or "estimate" label on every number, numbered findings, an open items section, one
recommendation paragraph at the end.

Then the body in Markdown. With the title on the cover, write sections as `## 1 Purpose`, `### 1.1 …` (they
become Heading 1 and 2). Without front matter the first `# Title` is the cover title.

| Write | You get |
|---|---|
| `## 2 Summary`, `### 2.1 Detail` | Headings in the heading font (numbered as you write them) |
| Paragraphs, `**bold**`, `*italic*`, `` `code` ``, `[text](https://…)` | Body text, links in the brand colour |
| `- item`, `1. step`; indent two spaces to nest | Lists |
| `| a | b |` + `|---|---|` | A table in the template's table style: header row, rules, figures in the number font, columns sized to content |
| `> One-line conclusion` | A pull-out with a coloured rule: use for the answer or a requirement |
| ```` ```svg Caption ```` … ```` ``` ```` or `![Caption](ref)` | Numbered, captioned figures (vector-art skill) |
| `<!-- pagebreak -->` | A page break |

Structure: purpose → answer or recommendation → detail → next steps with owners and dates. Short paragraphs,
tables for comparisons and registers. Engineering wording follows the organisation's engineering writing style.

`classification:` replaces the footer's default marking (e.g. `Internal`) only when the document needs a
different marking.

## Letters (.docx on the letterhead)

```
---
type: Letter
date: 16 October 2026
to: Jane Citizen; Project Director; Example Pty Ltd; 1 Example St, Exampletown 0000
dear: Jane
subject: Access to the site for the cooling survey
from_name: Sam Taylor
from_role: Chief Technology Officer
from_email: sam.taylor@example.com
from_phone: +00 000 000 000
---
We are writing to arrange access to …
```

- `to` is the address block: lines split on `;`.
- `dear` defaults to the first word of `to`.
- `from_email` and `from_phone` fill the letterhead's contact lines; a line you leave out is removed. Never put
  your own (the agent's) details on a letter signed by a person.
- The body is plain paragraphs: the letter-writing rules are below.

**Writing a letter:**
- The subject line states the point, in sentence case.
- Open with why you're writing, in one or two plain sentences: what the reader should know or do.
- Middle paragraphs give the supporting detail, one idea each.
- Close with the next step and who takes it.

**Sending:** a letter goes out under a person's name. Draft it for them to sign; don't send it yourself.

## Excel (.xlsx)

- Quick: one Markdown table (or CSV). Several sheets: `## Sheet name` above each table; a `# Title` line puts a
  title above a single sheet.
- Exact: JSON `{"sheets": [{"name", "title"?, "columns": ["Item", {"header": "Cost", "format": "currency0",
  "width": 14}], "rows": [[...]], "totals": {"sum": ["Cost"]}, "notes": ["Excl. tax"]}]}`.
- Values: `=SUM(B2:B9)` stays a formula; `$1,250.50`, `45%`, `2026-10-14` and `1,234` become real numbers, percents
  and dates. Formats: currency, currency0, percent, date, integer, number, text.
- Use formulas for totals and anything derived, so the person can change an input and the sheet follows. Excel
  recalculates when they open it.
- Charts:
  - start greyscale, and colour only the series that carries the point, in the brand's accent colour first;
  - the title states the claim and the number ("Revenue up 40%"), not the topic ("Revenue analysis");
  - only brand colours and fonts, never new ones.

## PowerPoint (.pptx)

Markdown:

```
# PRJ-1 *programme* update             *word* = the master's italic accent
Where we are, October 2026             subtitle
## Headlines
- On track for June energisation
  - Offer due November
## Long-lead items
| Package | Lead time | Budget |
|---|---|---|
| Transformer | 36 wk | $2.4m |
---
> Power first, then compute.           a section divider
## Key figures
- **36 wk** transformer lead time
- **10 MW** first build
## Spend by month
```chart bar
Month,Spend $k
Jul,120
Aug,340
```
## Site
![The north yard](outbox:…)           a picture slide (the slide's other text is the body)
Notes: say the yard is cleared         speaker notes
## Questions?                          closing slide
```

Or JSON `{title, subtitle?, author?, slides: [{kind, ...}]}` with kinds: bullets, two_column, section,
statement, image, comparison (`left/right: {label, headline, body}`), table, numbers (`items: [{value, label}]`,
up to 3), process (`steps`, up to 3), timeline (`items: [{date, heading, body, current?}]`, up to 4), chart
(`chart: bar|line, categories, series`), closing. Long content flows onto "(cont.)" slides.

One idea per slide, a claim as the title ("Order transformers by November"), not a topic ("Transformers"). At most
7 bullets. brand__make_deck / make_one_pager do the same from a slide or section spec.

## Look before you send

create_file and edit_file return a preview of the first page (or first three slides). Look at it: overflowing
text, a table too wide, an empty slide, a wrong number. Fix and rebuild before sending. preview_file shows any
other page (`pages: [3, 4]`). The preview's fonts may be open look-alikes of the brand fonts; in Word they'll be
the real ones.

## Editing someone's document

1. `file_outline` first: headings, tables (numbered from 1), sheets, slides.
2. `edit_file` with a list of edits (see the tool for every op). Always a new file; the original is untouched.
3. **Someone else's document: `track_changes: true`**, so they accept or reject each change in Word. Put questions
   and suggestions as `{op: comment, on: "exact text", text: "…"}`, not as edits.
4. Your own draft: plain edits are fine.
5. Excel: if the workbook has charts, pivot tables or images, edit_file refuses (they'd be lost). Ask the person; with
   their OK pass `allow_losses`, otherwise describe the change for them to make.
6. Say what changed (the tool's list) and where the new file went. Replacing the original in SharePoint asks first.

## Figures in existing files

`insert_image(document, image or svg, after, caption)`: after a heading or text, in place of a `[Figure: …]`
placeholder, or on a new slide at the end of a deck.
