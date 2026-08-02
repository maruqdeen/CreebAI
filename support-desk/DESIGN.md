# Design — Support Desk

Mode: **Operate**. The operator is in a task. Familiarity is a feature; the
point of view lives in the material, not in invented affordances.

## The world: a duty log

A support desk keeps a log. Not a dashboard — a **ruled ledger**: the bound
book a dispatcher, switchboard operator, or duty officer keeps of every call
received and how it was disposed of. Greenbar paper, oxide-red column rules,
iron-gall ink, and a rubber stamp for the disposition.

The world earns its place because every device maps to a real UI job:

| Ledger device | UI job |
|---|---|
| Alternating greenbar bands | Zebra striping on dense rows |
| Oxide-red column rules | Table column separators, section dividers |
| Rubber stamp | Status badge (`UNANSWERED`, `ANSWERED`, `FILED`) |
| **Carbon copy** | Anything the machine wrote — the Groq draft reply |
| Ruled entry line | Text input and textarea |
| Index tabs down the fore-edge | Primary navigation |
| Summary box, double-ruled | Stat tile |
| Form number in the corner | Page identity / route label |

**Carbon copy is the signature.** The bot's draft reply renders as a carbon
duplicate — aniline blue-violet ink on tinted flimsy, slightly offset, with a
`CARBON` tab. It is never black ink, because black ink is what the operator
writes. The moment the operator edits the draft it converts to black ink and
the carbon flag clears. That single rule carries product principle 2 across the
whole surface.

**Refused:** the category default (white ground, Inter, soft-shadow rounded
cards, violet accent, sparkline tiles) and its predictable opposite (near-black
console with a neon accent). Neither appears here.

## Color

Strategy: **full palette, four named roles** on a committed paper ground.
Chroma is spent on state only. Ground and rules stay low-chroma.

Defined in `src/app.css` as `@theme` tokens.

**Ground / structure**
- `--color-paper` `oklch(0.964 0.017 148)` — greenbar page ground
- `--color-band` `oklch(0.934 0.028 150)` — alternating band on dense rows
- `--color-leaf` `oklch(0.988 0.005 140)` — card, field, and panel face
- `--color-rule` `oklch(0.60 0.115 32 / 0.34)` — oxide-red column rule
- `--color-hair` `oklch(0.855 0.021 150)` — hairline divider
- `--color-edge` `oklch(0.795 0.028 150)` — solid border

**Ink ramp** (iron-gall blue-black, never pure neutral)
- `--color-ink` `oklch(0.255 0.036 254)` — body, headings, values
- `--color-ink-2` `oklch(0.435 0.030 254)` — secondary, meta
- `--color-ink-3` `oklch(0.515 0.027 254)` — captions, placeholders. This is the AA floor, set against `--color-band` (4.66:1), not against the lighter page ground; captions and log refs sit on striped rows.

**State roles**
- `--color-stamp` `oklch(0.545 0.185 32)` — vermilion. Unanswered, destructive, the email channel mark.
- `--color-indigo` `oklch(0.395 0.105 258)` — prussian. Primary action, the telegram channel mark, header band.
- `--color-carbon` `oklch(0.445 0.135 285)` — carbon violet. **Machine-written text only.** Never a button, never decoration.
- `--color-filed` `oklch(0.455 0.095 152)` — ledger green. Answered, filed, success.

Rules that bind:
- Carbon violet may not be used for any control. It marks provenance.
- No accent color on an inactive or disabled control.
- Every state ships a mark or label alongside its color.

## Typography

Two faces, split by *who wrote it*.

- **Public Sans** (variable, 300–800) — the whole interface: nav, labels,
  headings, body, buttons, data. A records-and-forms grotesque; it belongs to
  the world without being a costume.
- **Courier Prime** (400/700) — only what a machine typed: message IDs,
  timestamps, counts in the log margin, keyword tokens, and the carbon draft
  body. Never a UI label, never a button, never a heading.

Fixed rem scale, ratio ≈ 1.18. No fluid clamp sizing.

`--text-micro` .6875 / `--text-2xs` .75 / `--text-xs` .8125 / `--text-sm` .875
/ `--text-base` .9375 / `--text-lg` 1.0625 / `--text-xl` 1.375 /
`--text-stat` 2.5 / `--text-stat-lg` 3.25 (all rem)

Micro labels are uppercase, `0.09em` tracking, weight 600, `--color-ink-3` —
the printed caption above a ledger column. Prose caps at 68ch; table rows may
run wider.

## Layout

- Fixed 15rem index-tab sidebar ≥ 1024px; collapses to a top tab strip below.
- Content is a ruled page: 1.5rem gutter, 88rem max, column rules drawn with
  `border-inline` on the grid, not on cells.
- Dense rows are 3rem tall with a 2px channel mark on the leading edge.
- Spacing rhythm: 4 / 8 / 12 / 16 / 24 / 32 / 48. More space above a heading
  than below it.
- The composer is a right-side `<dialog>` panel at 34rem, full height, so it
  escapes every `overflow` ancestor. It is not a centered modal.

## Motion

150–220 ms, `cubic-bezier(.2,.7,.3,1)`. Motion reports state and nothing else:
drawer slide, stamp press, row file-away, toast. No page-load choreography —
the panel loads into a task. All of it disabled under
`prefers-reduced-motion`.

The one authored moment: filing a reply to the knowledge base **stamps** — the
badge scales down from 1.15 with a 40 ms settle, once. It is the only flourish
on the surface and it marks the product's central action.

## Components

Every interactive component ships default / hover / focus-visible / active /
disabled / loading. The queue ships an empty state that teaches (what the queue
is, why it is empty, where to add knowledge), not "nothing here".

Buttons: 1px `--color-edge` border, 2px radius, no shadow. Primary is filled
indigo. Ghost is transparent on the paper. There is one button shape on this
surface.

## Scene, and why the page is light

A solo operator at a desk in the evening, next to their own inbox and their own
Telegram window — both light. The subject is a paper record. Light ground, one
theme, no dark variant in this pass.
