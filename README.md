# creebai

An AI support agent for a Telegram community, with an admin panel.

It answers questions in the group from a knowledge base you write, using a Groq
model. Anything it cannot answer it stays quiet about and reports to you,
together with the terms it did not recognise — so the knowledge base grows from
what people actually ask.

## The two halves

| | |
|---|---|
| [`bot/`](bot) | Python. The Telegram bot, the answering pipeline, and the HTTP API. [Read this first.](bot/README.md) |
| [`support-desk/`](support-desk) | The admin panel: dashboard, queue, reply composer, settings. [Design notes.](support-desk/DESIGN.md) |

## How it decides

```
ignore → acknowledgement → guided flow → keyword gate → model → rules → threshold
```

- **Answers are quoted, not written.** The model is told to reproduce the
  knowledge base's own wording — steps, numbers, menu labels and links intact.
  A reworded instruction is a wrong instruction.
- **Guided flows ask before answering.** "My VPN is not working" has a dozen
  causes, so the bot collects the region, the app version and a screenshot
  first, then either resolves it or hands over with everything already gathered.
- **Failure is closed.** A model timeout, an auth error or an unreadable
  response all escalate to a human rather than guess.
- **Filing an answer closes the loop.** Replying from the panel writes the
  answer into the knowledge base *and* promotes the terms that made it a
  discovery, so the same question stops reaching the queue.

## Running it locally

```bash
cd bot && python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp bot/.env.example bot/.env
```

Fill in the bot token, the Groq key and an admin login — see
[bot/README.md](bot/README.md) for where each comes from.

```bash
cd bot && .venv/bin/python -m app.main
```

```bash
cd support-desk && npm install && npm run dev
```

The panel is then at http://localhost:5190.

Try the pipeline without Telegram at all:

```bash
cd bot && .venv/bin/python -m app.simulate "how do i connect on android"
```

## Deploying

[`render.yaml`](render.yaml) defines both services; the database is Neon
Postgres. Full instructions, including what each secret is for, are in
[bot/README.md](bot/README.md#deploying).

Production uses Telegram webhooks rather than polling, because free instances
sleep and only inbound traffic wakes them. Locally it stays on polling, so
development needs no public URL.

## Security

Every `/api` route requires a session — the panel reads real user messages and
can rewrite what the bot says. Only the health check and the login endpoint are
open, and the Telegram webhook carries its own secret.

Credentials live in the environment and never in this repository. Generate the
admin password hash with `python -m app.adminpw`; the password itself is never
stored or transmitted.

## Tests

```bash
cd bot && .venv/bin/python -m pytest tests -q
```
