# Support Desk — admin panel UI

The admin panel for an AI support agent that answers user questions,
complaints, and enquiries on a Telegram group bot and a support email inbox,
using a Groq model.

**This panel is live.** Every figure comes from the Python service in
[`../bot`](../bot); nothing is invented client-side. Start that service first,
or the pages will tell you they cannot reach it rather than render zeroes that
would read as facts:

```bash
cd bot && .venv/bin/python -m app.main
```

The API base is set by `<meta name="api-base">` in `partials/head.html`. It
becomes empty — same origin — once Laravel serves these pages itself.

Product truth is in [PRODUCT.md](PRODUCT.md); the visual system is in
[DESIGN.md](DESIGN.md). Read DESIGN.md before changing colour, type, or
component shape — the rules there are load-bearing (in particular: carbon
violet marks machine-written text and nothing else).

## Run it

```bash
npm install --prefix support-desk && npm run dev --prefix support-desk
```

Also registered in `.claude/launch.json` as `support-desk` on port 5190.

## Screens

| File | Route it becomes | Contents |
|---|---|---|
| `index.html` | `/` | Totals for both channels, week/month toggle, most-asked columns, digest schedule |
| `email.html` | `/email` | Email totals, most-asked of month, unanswered queue |
| `telegram.html` | `/telegram` | Telegram totals, most-asked of month, unanswered queue |
| `settings.html` | `/settings` | Telegram and Email tabs: connection, knowledge base, target keywords, rules, reply threshold, digest |

`partials/composer.html` is the reply drawer, shared by both queue pages. It
shows the original message, why the bot escalated it, the Groq draft rendered
as a carbon copy, and a **File to the knowledge base** step so answering a
question once removes it from the queue for good.

## Porting to Laravel

The markup is written to move into `resources/views/` without a rewrite.

1. Rename each page to `resources/views/<name>.blade.php`, partials to
   `resources/views/partials/<name>.blade.php`.
2. The include comments are already Blade syntax — delete the comment markers:

   ```diff
   - <!-- @include('partials.sidebar', {"nav":"telegram"}) -->
   + @include('partials.sidebar', ['nav' => 'telegram'])
   ```

   Partials already read their data as `{{ $nav }}`.
3. Move `src/app.css` and `src/app.js` under `resources/`, point
   `vite.config.js` at them with `laravel-vite-plugin`, and replace the
   `<script type="module" src="/src/app.js">` in `partials/head.html` with
   `@vite(['resources/css/app.css', 'resources/js/app.js'])`.
4. Turn each hard-coded list into a `@foreach`. The three that matter:
   the queue rows in `email.html` / `telegram.html`, the most-asked columns in
   `index.html`, and the rules list in `settings.html`.
5. Delete the `bladeIncludes()` plugin from `vite.config.js` — Blade does that
   job from then on.

## Backend seams

Every stub in `src/app.js` is commented with what replaces it.

| Interaction | Replace with |
|---|---|
| Reply → **Send** | `POST /api/queries/{ref}/reply` — sends via the Telegram Bot API or the mail provider, and, when *File to knowledge base* is ticked, appends the entry |
| Reply → **Redraft** | `POST /api/queries/{ref}/redraft` — Python worker calls Groq with the channel's knowledge base as system context |
| Settings → **Save** | Standard Laravel form POST per channel |
| Settings → **Test** | Ping the Python worker's connection check |
| Dashboard week/month | Controller round-trip on `?period=` rather than the client-side swap |
| Queue filter | Fine to keep client-side until the queue outgrows one page |

The escalation logic the UI already assumes: the worker scores each incoming
message against the channel's knowledge base, replies itself above the
configured threshold, and writes anything below it — plus anything a rule
holds back — into the queue with its confidence and reason.

## Where the data comes from

No synthetic data remains. Each surface reads the service:

| Surface | Endpoint |
|---|---|
| Dashboard totals, channel split, most asked | `GET /api/dashboard?period=` |
| "Asked about, not in the knowledge base" | the same response's `new_keywords` |
| Queue rows and filters | `GET /api/queries?channel=&state=` |
| Composer → Send | `POST /api/queries/{ref}/reply` |
| Composer → Redraft | `POST /api/queries/{ref}/redraft` |
| Settings load / save | `GET` + `PUT /api/settings/{channel}` |
| Derive from knowledge base | `POST /api/settings/{channel}/keywords/suggest` |
| On Duty block, connection status | `GET /api/health` |

Rows are cloned from `<template>` elements that hold the real markup, so the
row stays designed in HTML and ports to a Blade `@foreach` unchanged.

**Secrets are not editable here, deliberately.** The BotFather token and Groq
key live in `bot/.env`. A credential typed into this page would have to travel
over the network and be stored to be useful, so the connection panel shows
status from `/api/health` and explains where the real setting is.

**Delivery is reported honestly.** If a reply saves but Telegram refuses it,
the toast says so and names the error rather than claiming a send that did not
happen.

## Known gaps

- Light theme only, by decision — see the closing section of DESIGN.md.
- No auth screens; the panel assumes a signed-in operator.
- Single-operator by design. Status and assignee have no UI, though `data-state`
  on each queue row is the hook if a second operator is ever added.
