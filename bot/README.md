# Support Desk — Telegram bot

The working half of the support system: a bot that sits in your Telegram group,
answers questions from a knowledge base you write via a Groq model, and captures
everything it *cannot* answer so you find out about it.

The admin panel UI lives beside this at [`../support-desk`](../support-desk).
It is still static; this service is the API it will read.

## How it decides

```
ignore → acknowledgement → flow → keyword gate → model → rules → threshold
```

**Product scope** is an optional extra gate, currently **off**. Filling in
`product_terms` in `seed.yaml` makes the bot ignore any message that mentions
none of them — useful in a busy group where generic keywords like "time",
"data" or "free" drag ordinary conversation into the queue. It was tried and
turned off, because it also drops legitimate questions that never name the
product ("is there a free trial?"). Messages continuing a guided flow are
exempt from it either way.

1. **Ignore** — bot messages, commands, and anything without text.
2. **Keyword gate** — does the message contain one of your target keywords?
   - **Yes** → the model is asked.
   - **No, but it reads like a question or a complaint** → *silent capture*: no
     reply, **no model call, no cost**, but the question is logged with the new
     terms it introduced. This is what stops a pure keyword gate from hiding the
     brand-new topics you most want to discover.
   - **No, and it's ordinary chatter** → dropped, nothing stored.
3. **Model** — one Groq call returning a strict-schema verdict:
   `{answer, confidence, covered_by_kb, topic, keywords}`.
4. **Rules** — a rule with trigger terms holds the message for you regardless of
   what the model said. Rules run *after* the model so you still get a draft.
5. **Threshold** — below your reply threshold, or not covered by the knowledge
   base, the bot stays quiet and queues it.

Anything queued gets a DM to you with the question, why it escalated, and any
terms that are not in your knowledge base yet.

**Failure is closed.** A Groq timeout, an auth error, or an unreadable response
all escalate to you. The bot going quiet is a small cost; the bot inventing an
answer about someone's money is not.

## Guided flows — asking before answering

Some complaints are too vague to answer well. "My VPN is not working" has a
dozen causes, and a generic reply helps nobody. [`flows.yaml`](flows.yaml)
defines scripts the bot follows instead: it asks for what it needs, one
question at a time, then either resolves the problem or hands over with
everything already collected.

Flows are checked **before** the keyword gate and before the model, so they
cost nothing and run exactly as written — the model is never asked to
improvise the questions or reorder them.

While someone is mid-flow their next message goes to the flow whatever it
says, so an answer like "Nigeria MTN 50mb" is understood even though it
carries no target keyword. Flows run per person, so two people in the group
can be in one at the same time. An unanswered flow lapses after
`timeout_minutes` (30 by default) so nobody is trapped in it, and `/cancel`
leaves one at any point.

When a flow ends without resolving anything, the queue row carries the whole
transcript — the admin opens it already knowing the region, the app version
and what was tried. The same transcript goes into the escalation DM.

Editing `flows.yaml` needs a restart. Moving flows into the database so the
panel can edit them is the obvious next step; it is not built.

## The loop that shrinks the queue

Answering from the panel with *File to the knowledge base* does two things:

1. Writes a knowledge base entry the model reads on every later message.
2. Promotes the terms that made it a discovery into your target keywords (up to
   five per reply), so the same question clears the gate next time.

Both are needed. Without the first the bot never learns the answer; without the
second the question keeps landing in your queue forever. Promoted keywords are
ordinary keywords — remove any you don't want from the settings page.

## Model

`openai/gpt-oss-120b`, set by `GROQ_MODEL`.

Only `openai/gpt-oss-120b` and `openai/gpt-oss-20b` support **strict
`json_schema`** on Groq; everything else falls back to `json_object`, which is
valid JSON with no schema enforcement. The escalation decision needs a typed
verdict, so strict mode is the mechanism here, not a nicety.

Do not use `llama-3.3-70b-versatile` — it shuts down **2026-08-16**.

- <https://console.groq.com/docs/structured-outputs>
- <https://console.groq.com/docs/deprecations>

## Setup

Only you can do these — I will not enter credentials or create accounts.

1. **Create the bot.** Message [@BotFather](https://t.me/botfather), `/newbot`,
   copy the token.

2. **Turn group privacy OFF.** BotFather → `/mybots` → your bot → Bot Settings →
   Group Privacy → **Turn off**.

   > **Then remove the bot from the group and add it again.** The change does
   > not apply to an existing membership. Skip this and the bot sees nothing but
   > direct @mentions, the keyword gate never fires, and it will look broken.

   Run `/diag` in the group to confirm it can see messages.

3. **Get your Telegram user id** from [@userinfobot](https://t.me/userinfobot).

4. **Get a Groq API key** from <https://console.groq.com>.

5. **Fill in `.env`:**

   ```bash
   cp bot/.env.example bot/.env
   ```

6. **`/start` the bot in a direct message.** A Telegram bot cannot open a
   conversation with you, so until you do this it has no way to send you
   anything. Queries are still captured meanwhile — only the notification is
   missed, and the log says so.

7. **Rewrite `seed.yaml` for your product.** What ships is a placeholder
   describing a generic payouts app. The model treats the knowledge base as
   fact, so wrong text there becomes wrong answers to your users.

## Running

```bash
python -m venv bot/.venv && bot/.venv/bin/pip install -r bot/requirements.txt
```

```bash
cd bot && .venv/bin/python -m app.seed
```

```bash
cd bot && .venv/bin/python -m app.main
```

`--api-only` runs just the panel API (no bot token needed); `--bot-only` runs
just the bot. Re-running the seed will not overwrite a knowledge base you have
edited unless you pass `--force`.

## Loading your knowledge base

**Keep it as plain text.** The knowledge base is read by a language model, which
reads prose natively. Converting it to JSON makes it *harder* for the model, not
easier — the only JSON here is the model's reply back to us, which is a
different thing.

```bash
cd bot && .venv/bin/python -m app.kb_import ~/creebvpn.txt
```

Or straight from the clipboard: `pbpaste | .venv/bin/python -m app.kb_import -`

The important half is the keywords. **A group message must match a target
keyword before the bot consults the knowledge base at all**, so a new knowledge
base with the old keywords still attached leaves the bot mute on its own
subject matter. The importer derives keywords from the text you just wrote,
shows you each one with how often it appears, and waits for confirmation before
saving. `--keep-keywords` replaces only the text.

The panel gets the same thing via
`POST /api/settings/{channel}/keywords/suggest`, which derives without saving so
suggestions can be shown beside the textarea.

## Trying it in Telegram before it joins a group

DM the bot anything (once you have `/start`ed it and your `ADMIN_TELEGRAM_USER_ID`
is set) and it replies with what it *would* do in the group — answer, stay quiet,
or ignore — plus the keyword hit, confidence, and any new terms. Nothing is
stored, so rehearsing never fills your queue.

Anyone who is not the admin gets pointed at the group instead. A bot that
answered strangers in private would bypass the keyword and rules model that
keeps it safe.

## Trying it without Telegram at all

`simulate` runs the entire pipeline on typed text — no Telegram, no bot token.
It rolls back by default, so you can poke at it as much as you like.

```bash
cd bot && .venv/bin/python -m app.simulate "when is the payout processed?"
```

Add `--dry-run` to skip the Groq call too, or `--keep` to actually store the row.

```bash
cd bot && .venv/bin/python -m pytest tests -q
```

## API

Matches the seams documented in `../support-desk/README.md`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | What is still unconfigured |
| GET | `/api/dashboard?period=week\|month` | Totals, channel split, most asked |
| GET | `/api/queries?channel=&state=` | Queue rows |
| GET | `/api/queries/{ref}` | One row |
| POST | `/api/queries/{ref}/reply` | Send, optionally file to the KB |
| POST | `/api/queries/{ref}/redraft` | Fresh draft from the model |
| GET | `/api/keywords/new` | Terms asked about that the KB does not cover |
| GET/PUT | `/api/settings/{channel}` | KB, keywords, rules, threshold |

Interactive docs at `http://localhost:8000/docs` while it is running.

## Layout

| File | What it does |
|---|---|
| `app/pipeline.py` | The decision. Telegram-free and fully tested. |
| `app/kb.py` | Keyword gate, question heuristic, prompt assembly. |
| `app/groq_client.py` | The single Groq call and the strict schema. |
| `app/telegram_bot.py` | Handlers, polling, `/start` `/status` `/diag`. |
| `app/notify.py` | The escalation DM. |
| `app/api.py` | HTTP API for the panel. |
| `app/models.py` | Tables, shaped from what the panel renders. |
| `seed.yaml` | Starting knowledge base, keywords and rules. **Placeholder.** |

## Deploying

Render (bot + panel) and Neon (Postgres). Both free.

**Why webhooks in production.** Free instances sleep after 15 minutes idle and
only inbound traffic wakes them; a polling loop would simply die. Setting
`PUBLIC_URL` switches modes — locally it stays blank and polling is used, so
development needs no public URL and no extra flags.

The trade-off is a cold start: after a quiet spell the first message takes about
a minute while the instance wakes. Telegram retries deliveries, so nothing is
lost; the reply is just late. A paid instance removes it.

### Before the first deploy

```bash
cd bot && .venv/bin/python -m app.adminpw
```

Prints `ADMIN_PASSWORD_HASH`, `SECRET_KEY` and `TELEGRAM_WEBHOOK_SECRET`. The
password itself is never stored or transmitted — only the scrypt hash.

### Deploy

1. Create a Neon project and copy its connection string.
2. In Render: **New → Blueprint**, point at this repo. `render.yaml` defines
   both services and prompts for each secret; nothing sensitive is committed.
3. Move your configuration across:

```bash
cd bot && .venv/bin/python -m app.migrate_config --to "postgresql://…" --apply
```

   That copies the knowledge base, keywords, product scope, rules and filed
   answers. It deliberately leaves queries and conversations behind — local
   test traffic in production would make the dashboard's first figures a lie.

4. Sign in at the panel URL with the username and password from step 1.

### What each secret is for

| Variable | Why |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY` | As locally |
| `DATABASE_URL` | The Neon string. Free Render web services have no disk, so SQLite cannot survive a deploy |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` | The panel login. Without them nobody can sign in — the panel is unusable rather than open |
| `SECRET_KEY` | Signs session tokens. Rotate it to sign everyone out |
| `TELEGRAM_WEBHOOK_SECRET` | Telegram echoes it on every delivery, so a guessed webhook URL is still rejected |
| `PUBLIC_URL` | Set by Render; switches polling → webhook |
| `EXTRA_CORS_ORIGINS` | The panel is a different origin from the API |

### Security

The panel reads every message your users send and can rewrite the knowledge
base the bot answers from, so **every `/api` route requires a session**. Only
`/api/health` (so the host can probe it) and `/api/auth/login` are open. The
Telegram webhook sits outside `/api` and carries its own secret.

Sessions last 12 hours. Tokens are HMAC-signed and held in `localStorage`
rather than a cookie, because the panel and API are separate origins.

## Not built yet

Deliberately out of scope for this phase; the data model already supports each.

- **Webhook mode.** Polling is right for development and needs no public URL.
- **The weekly/monthly FAQ digest sender.** The settings and the data are there;
  the scheduler is not.
- **Wiring the panel to this API.** The panel is still showing synthetic data.
- **The Laravel port.** These tables are designed for Laravel to read directly.
- **Retrieval over a chunked KB.** The whole knowledge base goes into the prompt.
  With a 131k context that is fine until it grows past roughly 40k tokens.

## Worth knowing

- **Confidence is self-reported.** Strict schema guarantees a number, not a
  calibrated one. The rules layer is what catches the genuinely risky messages,
  and the threshold is tunable live. Expect to tune it in the first week.
- **A bot replying in a group is public.** Review your keywords and rules before
  it joins the real group rather than a test one.
- **Nothing logs a token or an API key**, and no credential is ever written to
  the database.
