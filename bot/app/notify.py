"""Escalation DM to the admin.

A Telegram bot cannot open a conversation — the admin has to message it first.
Until they do, `admin_chat_id` is null and every escalation logs a loud, single
line telling them to `/start` it. The query is still queued either way; the
notification is the only thing that is missed, never the capture.
"""

import html
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode

from app.config import settings
from app.models import ChannelSettings, Query
from app.pipeline import Decision

log = logging.getLogger(__name__)

MAX_QUOTE = 500
MAX_TRANSCRIPT = 1200


def get_admin_chat_id(session: Session, channel: str) -> int | None:
    return session.scalar(
        select(ChannelSettings.admin_chat_id).where(ChannelSettings.channel == channel)
    )


def set_admin_chat_id(session: Session, channel: str, chat_id: int) -> None:
    cs = session.scalar(select(ChannelSettings).where(ChannelSettings.channel == channel))
    if cs is None:
        cs = ChannelSettings(channel=channel, kb_text="")
        session.add(cs)
    cs.admin_chat_id = chat_id


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def build_escalation_message(query: Query, decision: Decision) -> str:
    """The DM body. HTML parse mode, so every interpolated value is escaped."""
    body = query.body or ""
    if len(body) > MAX_QUOTE:
        body = body[:MAX_QUOTE].rstrip() + "…"

    why = decision.reason or query.reason or "Held for a human."
    if query.confidence is not None:
        why = f"{why} (confidence {query.confidence:.2f})"

    lines = [
        f"<b>{_esc(query.ref)}</b> · needs you",
        "",
        f"<b>{_esc(query.user_handle or 'someone')}</b> asked:",
        f"<blockquote>{_esc(body)}</blockquote>",
        "",
        f"<i>{_esc(why)}</i>",
    ]

    if query.transcript:
        # A guided flow already asked the obvious questions; put the answers in
        # front of the admin rather than making them open the panel to find them.
        collected = query.transcript
        if len(collected) > MAX_TRANSCRIPT:
            collected = collected[-MAX_TRANSCRIPT:].lstrip()
        lines += ["", f"<b>Already asked:</b>\n<blockquote>{_esc(collected)}</blockquote>"]

    new_terms = [k.term for k in query.keywords if k.is_new]
    if new_terms:
        tags = ", ".join(f"<code>{_esc(t)}</code>" for t in new_terms[:8])
        lines += ["", f"🔑 Not in your knowledge base yet: {tags}"]

    if query.draft:
        draft = query.draft
        if len(draft) > MAX_QUOTE:
            draft = draft[:MAX_QUOTE].rstrip() + "…"
        lines += ["", f"Draft ready in the panel:\n<blockquote>{_esc(draft)}</blockquote>"]

    panel = settings.panel_base_url.rstrip("/")
    lines += ["", f'<a href="{_esc(panel)}/telegram.html">Open the queue</a>']
    return "\n".join(lines)


async def notify_escalation(bot: Bot, query: Query, decision: Decision, admin_chat_id: int | None) -> bool:
    """DM the admin. Returns whether it was delivered."""
    if admin_chat_id is None:
        log.warning(
            "%s was queued but no admin chat is known — send /start to the bot in "
            "a direct message once so it can reach you.",
            query.ref,
        )
        return False

    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=build_escalation_message(query, decision),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:  # blocked, deleted chat, network
        log.error("Could not DM the admin about %s: %s", query.ref, exc)
        return False
