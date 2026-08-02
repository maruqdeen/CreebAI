# Product — Support Desk

> Scoped to `support-desk/`. The `PRODUCT.md` at the repository root belongs to
> the 2amazingHands crochet site and does not govern this app.

## Product Purpose

An AI support agent that answers user questions, complaints, and enquiries on
two channels — a Telegram group/channel bot and a support email inbox — backed
by a Groq LLM. This app is the **admin panel**: the place the operator teaches
the bot what to say, catches what the bot could not answer, and reads what
users keep asking.

The mechanism in one sentence: *the bot answers from a knowledge base you
write; everything it cannot answer lands in a queue, and every reply you write
by hand becomes knowledge base the bot uses next time.* The panel exists to
close that loop.

## Users

One operator — the app's founder or its single support person. They are not a
support professional and this is not their full-time job. They open the panel
once a day, usually in the evening, alongside a browser already full of their
own inbox and Telegram. They want two answers fast: *did the bot keep up
today,* and *what is it still getting wrong.*

Not a team product. No assignment, no ownership, no "claimed by". The markup
leaves room for status if a second operator is ever added, but the interface
assumes one pair of hands.

## Jobs

1. **Glance** — see volume answered across both channels and whether it is
   rising or falling.
2. **Clear the queue** — work the list of questions the bot could not answer,
   write a reply, and send it.
3. **Teach** — file that reply into the knowledge base so the same question
   never reaches the queue again.
4. **Configure** — connect the Telegram bot and the email inbox, write the
   knowledge base, set target keywords and response rules.
5. **Review** — read the week's and month's most-asked questions, which the bot
   also digests and sends to the admin on a schedule.

## Operating Context

Desktop-first, single operator, low frequency (daily), low volume (tens of
queries a day, not thousands). Latency is not the problem; *knowing what to
type* is. The panel should make the queue feel finite and the knowledge base
feel like the thing that shrinks it.

## Product Principles

1. **Every unanswered query is a gap in the knowledge base.** The interface
   should never let the operator send a reply without offering to file it.
2. **The bot's draft is a starting point, never an authority.** Machine-written
   text is visually marked as machine-written, everywhere it appears.
3. **Two channels, one desk.** Telegram and email get identical structure and
   identical controls; only the channel mark changes.
4. **Density over decoration.** The operator is reading rows of real user
   complaints. Nothing on screen competes with that text.
5. **State is legible without hovering.** Answered, unanswered, escalated, and
   filed are distinguishable at a glance and in monochrome.

## Brand Commitments

None inherited. This product has no prior visual identity.

## Accessibility

WCAG AA. Body text ≥ 4.5:1 on the paper ground, all state conveyed by mark and
label as well as color, full keyboard path through nav → queue → composer →
send, visible focus ring on every control, `prefers-reduced-motion` honored.

## Evidence on Hand

Hand-drawn wireframe of five screens (dashboard, email, telegram, settings ×2)
supplied by the user, 2026-07-29. All queue content, counts, and message bodies
currently in the build are **synthetic demonstration data** authored for the UI
pass — see `README.md` for the replacement list.

## Stack

Laravel (Blade + routes + controllers), Python worker for the Groq calls and
the Telegram/email pollers, vanilla JS, Tailwind CSS v4.
