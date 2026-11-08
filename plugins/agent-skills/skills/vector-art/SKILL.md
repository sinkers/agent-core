---
name: vector-art
description: "How to draw clear diagrams and illustrations as SVG (block and flow diagrams, single-line diagrams, site and rack layouts, timelines, simple charts, icons) and put them into Word documents, decks, emails or SharePoint. Use whenever someone asks for a diagram, drawing, sketch, figure, schematic, layout, illustration or picture you have to make yourself."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Vector art (SVG)

You draw by writing SVG markup. The tools check it, render it, and place it as a real vector picture: Word 2016+
and PowerPoint draw the SVG itself, and older readers get a PNG copy.

## Workflow

1. **Plan before drawing.** Decide what the figure must show, list its elements, and sketch a grid (columns,
   rows, spacing) in your head. One idea per figure.
2. **Write the SVG** following the rules below.
3. **Make it and look at it.** `files__create_file` with a `.svg` name returns a preview image. Check that
   text fits inside its boxes, nothing overlaps, arrows meet their targets, and it reads at a glance. Fix and
   create it again if not. Two rounds is normal.
4. **Put it where it's needed:**
   - a new Word document: a ```` ```svg Caption ```` block, or `![Caption](outbox:…)`, in the Markdown for
     `files__create_file` (see the office-documents skill);
   - an existing Word document or deck: `files__insert_image` with the document and `image` (the outbox
     reference) or `svg`, plus `after` (a heading, some text, or a `[Figure: …]` placeholder) and `caption`;
   - a picture on its own: `.svg` for people who edit it (Inkscape, Illustrator, Visio import), `.png` (from the
     same markup) for chat, email bodies and anything that won't take SVG.
   Then send or save it as usual (send_file, email_file, save_file_to_sharepoint).

## Rules for the markup

- Root: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 W H">`. Use a viewBox and design in its units.
  Good sizes: **800×450** (16:9, slides and wide figures), **800×600** (page figures), **600×600** (square).
  The page text width in Word is about 16 cm; an 800-wide drawing fills it.
- Everything is drawn in the file: no `<script>`, no external images, fonts or links (they are removed). Embedded
  `data:image/png` pictures work but defeat the point; prefer shapes.
- **Text:** `font-family="Calibri, Arial, sans-serif"` (Georgia for a title, if any). At 800 wide, labels are
  14–16, small notes 12, never below 11. Use `text-anchor="middle"` and `dominant-baseline="middle"` to centre
  a label in a box. SVG text doesn't wrap: break long labels into `<tspan x=".." dy="1.2em">` lines, and allow
  about 0.55 × font-size per character when sizing boxes.
- **Lines:** strokes 1.5–2 for structure, 1 for hairlines and leaders, 3 for emphasis. `stroke-linecap="round"`,
  `stroke-linejoin="round"`. Use `fill="none"` on lines and paths that aren't shapes.
- **Arrows:** define one marker and reuse it:
  `<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
  orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#222222"/></marker></defs>`, then
  `marker-end="url(#arrow)"`. End lines at the box edge, not its centre.
- **Reuse** with `<g>` and `<use href="#id">` for repeated symbols (racks, breakers, panels). Keep coordinates
  whole numbers. Group related parts with `<g id="…">` so people can edit them later.
- Background: leave it transparent for documents, or a full-size `<rect>` in the brand's page colour for a
  standalone figure.

## Colours

Use the organisation's brand: if a brand skill is available, load it for the palette and the chart series order.
Without one: text and outlines `#222222`, structure (main boxes, headers, busbars) `#1F4E79` with light panels
`#DEEAF6`, the one thing to notice (one per figure) `#C55A11`, secondary text and dimensions `#7F7F7F`. Flat fills,
generous white space, aligned edges; no gradients, drop shadows or 3-D.

## Figure types

- **Block / flow diagram:** boxes on a grid (e.g. 160×60, 40 apart), left to right or top to bottom, arrows
  between edges, a label on each arrow only if it adds something. No more than about 12 boxes; split otherwise.
- **Single-line diagram (SLD):** follow the IEC 60617 conventions: busbars as thick horizontal lines (stroke 4) with
  the voltage at the left end; a transformer as two overlapping circles with the ratings beside it; a circuit
  breaker as a small square, a disconnector as an angled line; a cable as a line with its size and length
  beside it; loads as an arrow or labelled box at the bottom. Power flows top to bottom. Label each item with
  its designation (e.g. `CB-01`, `TX-1 2.5 MVA 33/0.415 kV`). Mark it **indicative, not for construction** unless
  an engineer has checked it.
- **Site or rack layout:** draw to scale. State the scale (e.g. 1 unit = 0.1 m), add a scale bar and a north
  arrow, dimension the key distances in Shale with leader lines, and label every item. Keep clearances visible.
- **Timeline / phases:** a horizontal axis with dated ticks and phase bars. For real schedules use the plan tool
  instead (plan__make_plan): it calculates; a drawing doesn't.
- **Simple chart:** only from real figures you have, with the source in the caption. Axes start at zero for bars,
  values labelled directly rather than with a legend where possible. For anything more than a few bars, build a
  spreadsheet or table instead.
- **Icon or illustration:** a 64×64 or 128×128 viewBox, 2–4 flat shapes, one colour plus Charcoal.

## Before you send

- Text inside its boxes, nothing overlapping, nothing cut off at the edges (keep a 20-unit margin).
- Every label spelt right and matching the document's names and designations.
- A caption that says what the figure shows (and its source, for data).
- For engineering content, say it's a drawing you made, and what it was based on. It is not a controlled
  drawing until it's issued through document control.
