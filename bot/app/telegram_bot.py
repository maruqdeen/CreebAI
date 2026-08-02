"""The Telegram bot.

Polling mode: right for development, and it needs no public URL. Webhook mode
is a later phase.

**The bot must have group privacy turned OFF** or it never sees ordinary group
messages and the keyword gate can never fire. BotFather → /mybots → your bot →
Bot Settings → Group Privacy → Turn off, then remove and re-add the bot to the
group — the change does not apply to an existing membership. `/diag` in the
group reports whether it is actually seeing messages.

Threading: the pipeline is synchronous (SQLAlchemy, and a Groq call that takes
about a second). It runs in a worker thread via `asyncio.to_thread` so a slow
model call never stalls the event loop and the bot stays responsive.
"""

import asyncio
import html
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.db import SessionLocal, init_db, session_scope
from app.groq_client import GroqJudge
from app.kb import match_keywords
from app.models import TELEGRAM, Query
from app.notify import get_admin_chat_id, notify_escalation, set_admin_chat_id
from app.pipeline import (
    IGNORE,
    QUEUE,
    REPLY,
    ChannelContext,
    Decision,
    Inbound,
    advance_flow,
    close_on_acknowledgement,
    decide,
    handle,
)

log = logging.getLogger(__name__)

# Telegram rejects anything longer.
TELEGRAM_MAX_CHARS = 4096


def split_for_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Break a long answer into sendable pieces without losing any of it.

    Answers now reproduce whole procedures from the knowledge base, so they can
    exceed Telegram's limit. Truncating would silently drop the last steps —
    the ones someone is about to follow — so split instead, preferring
    paragraph then line then sentence boundaries so a numbered list never
    breaks mid-step.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
        )
        # No usable boundary (one enormous unbroken run): fall back to a hard
        # cut rather than looping forever.
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks

_judge = GroqJudge()


@dataclass
class Processed:
    """What the async layer needs after the sync pipeline has run.

    The ORM objects belong to a session that is closed by the time we get here,
    so the values are copied out rather than passed by reference.
    """

    action: str
    reason: str
    reply_text: str | None
    query_id: int | None
    query_ref: str | None
    admin_chat_id: int | None


def _process(inbound: Inbound) -> Processed:
    """Synchronous: own session, own transaction. Runs in a worker thread."""
    with session_scope() as session:
        decision = handle(inbound, session, _judge)
        admin_chat_id = get_admin_chat_id(session, inbound.channel)
        return Processed(
            action=decision.action,
            reason=decision.reason,
            reply_text=decision.reply_text,
            query_id=decision.query_id,
            query_ref=decision.query_ref,
            admin_chat_id=admin_chat_id,
        )


def _load_for_notify(query_id: int):
    """Re-read the query and rebuild a Decision shim for the DM body."""
    with session_scope() as session:
        query = session.get(Query, query_id)
        if query is None:
            return None, None
        # Touch the relationship inside the session so it is loaded.
        _ = list(query.keywords)
        session.expunge(query)
        return query, Decision(QUEUE, query.reason or "")


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    # Photos matter because a flow can ask for a screenshot; the caption is the
    # description that comes with it.
    has_photo = bool(message.photo or message.video or message.document)
    text = (message.text or message.caption or "").strip()
    if not text and not has_photo:
        return

    chat = update.effective_chat
    if settings.telegram_group_chat_id and chat.id != settings.telegram_group_chat_id:
        log.debug("Ignoring message from unpinned chat %s", chat.id)
        return

    user = message.from_user
    inbound = Inbound(
        text=text,
        channel=TELEGRAM,
        chat_id=chat.id,
        message_id=message.message_id,
        user_id=user.id if user else None,
        user_handle=(f"@{user.username}" if user and user.username else (user.full_name if user else None)),
        from_bot=bool(user and user.is_bot),
        has_photo=has_photo,
    )

    try:
        result = await asyncio.to_thread(_process, inbound)
    except Exception:
        # Never let one bad message kill the handler loop.
        log.exception("Pipeline raised on message %s in chat %s", message.message_id, chat.id)
        return

    if result.action == IGNORE:
        return

    if result.action == REPLY and result.reply_text:
        parts = split_for_telegram(result.reply_text)
        try:
            for index, part in enumerate(parts):
                # No parse_mode: model output is plain text and must never be
                # interpreted as markup. Only the first part replies to the
                # original message; the rest follow it.
                if index == 0:
                    await message.reply_text(part, disable_web_page_preview=True)
                else:
                    await context.bot.send_message(
                        chat_id=chat.id, text=part, disable_web_page_preview=True
                    )
            log.info(
                "%s answered in %s%s",
                result.query_ref,
                chat.id,
                f" ({len(parts)} messages)" if len(parts) > 1 else "",
            )
        except Exception:
            log.exception("Could not post the reply for %s", result.query_ref)
        return

    # Queued: stay quiet in the group, tell the admin.
    log.info("%s queued — %s", result.query_ref, result.reason)
    if result.query_id is not None:
        query, decision = await asyncio.to_thread(_load_for_notify, result.query_id)
        if query is not None:
            await notify_escalation(context.bot, query, decision, result.admin_chat_id)


def _is_admin_chat(chat_id: int, user_id: int | None) -> bool:
    """Whether this private chat belongs to the admin.

    In a Telegram private chat the chat id *is* the user's id, so the recorded
    `admin_chat_id` doubles as the admin's identity — no second column needed.

    An explicit ADMIN_TELEGRAM_USER_ID always wins. With none configured, the
    seat goes to whoever claimed it first via /start.
    """
    if settings.admin_telegram_user_id is not None:
        return user_id == settings.admin_telegram_user_id
    with session_scope() as session:
        recorded = get_admin_chat_id(session, TELEGRAM)
    return recorded is not None and recorded == chat_id


def _claim_admin(chat_id: int) -> bool:
    """Take the admin seat if it is still empty. True when this call took it."""
    with session_scope() as session:
        if get_admin_chat_id(session, TELEGRAM) is not None:
            return False
        set_admin_chat_id(session, TELEGRAM, chat_id)
        return True


@dataclass
class Rehearsal:
    """What the bot *would* do with a message, without doing any of it."""

    action: str
    reason: str
    reply_text: str | None
    confidence: float | None
    covered_by_kb: bool | None
    matched: list[str]
    new_terms: list[str]
    model_error: str | None


def _rehearse(inbound: Inbound) -> Rehearsal:
    """Run the real pipeline, then throw the transaction away.

    A rehearsal must not fill the queue with the operator's own test messages,
    so nothing is persisted and no admin DM is sent.

    Guided flows are the exception: they are stateful, so a rolled-back
    rehearsal could never reach step two. They run for real against this DM,
    but without filing a queue row when they escalate.
    """
    with session_scope() as ack_session:
        ack = close_on_acknowledgement(inbound, ack_session)
    if ack is not None:
        return Rehearsal(
            action=ack.action,
            reason=ack.reason,
            reply_text=ack.reply_text,
            confidence=None,
            covered_by_kb=None,
            matched=[],
            new_terms=[],
            model_error=None,
        )

    with session_scope() as flow_session:
        flow = advance_flow(
            inbound, flow_session, judge=_judge, log_escalation=False
        )
    if flow is not None:
        return Rehearsal(
            action=flow.action,
            reason=flow.reason,
            reply_text=flow.reply_text,
            confidence=None,
            covered_by_kb=None,
            matched=[],
            new_terms=[],
            model_error=None,
        )

    session = SessionLocal()
    try:
        ctx = ChannelContext.load(session, inbound.channel)
        matched = match_keywords(inbound.text, ctx.keywords)
        decision = decide(inbound, session, _judge)
        verdict = decision.verdict
        return Rehearsal(
            action=decision.action,
            reason=decision.reason,
            reply_text=decision.reply_text,
            confidence=verdict.confidence if verdict and verdict.ok else None,
            covered_by_kb=verdict.covered_by_kb if verdict and verdict.ok else None,
            matched=matched,
            new_terms=decision.new_keywords,
            model_error=verdict.error if verdict else None,
        )
    finally:
        session.rollback()
        session.close()


def _format_rehearsal(r: Rehearsal) -> str:
    """Readable in a phone-sized chat window. HTML, so values are escaped."""
    esc = html.escape

    if r.action == REPLY:
        head = "✅ <b>In the group I would answer this:</b>"
        body = f"\n\n{esc(r.reply_text or '')}"
    elif r.action == QUEUE:
        head = "🔒 <b>In the group I would stay quiet</b> and send it to your queue."
        body = ""
    else:
        head = "😶 <b>In the group I would ignore this.</b>"
        body = ""

    detail = [f"\n\n<i>{esc(r.reason)}</i>"]

    facts = []
    facts.append(
        "keyword hit: " + (", ".join(r.matched) if r.matched else "none")
    )
    if r.confidence is not None:
        facts.append(f"confidence: {r.confidence:.2f}")
    if r.covered_by_kb is not None:
        facts.append(f"in knowledge base: {'yes' if r.covered_by_kb else 'no'}")
    if r.new_terms:
        facts.append("new terms: " + ", ".join(r.new_terms[:6]))
    detail.append("\n\n<code>" + esc(" · ".join(facts)) + "</code>")

    if r.model_error:
        detail.append(f"\n\n⚠️ model error: <code>{esc(r.model_error)}</code>")

    detail.append("\n\n<i>Rehearsal — nothing was stored.</i>")
    return head + body + "".join(detail)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rehearsal in the admin's DM: try the bot out before it joins a group.

    Anyone who is not the admin gets pointed at the group instead — the bot
    answering strangers privately would bypass the whole keyword-and-rules
    model that keeps it safe.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None:
        return

    has_photo = bool(message.photo or message.video or message.document)
    text = (message.text or message.caption or "").strip()
    if not text and not has_photo:
        return

    if not await asyncio.to_thread(_is_admin_chat, update.effective_chat.id, user.id if user else None):
        await message.reply_text(
            "I answer support questions in the group — ask there and I'll pick it up."
        )
        return

    inbound = Inbound(
        text=text,
        channel=TELEGRAM,
        chat_id=update.effective_chat.id,
        message_id=message.message_id,
        user_id=user.id,
        user_handle=(f"@{user.username}" if user.username else user.full_name),
        has_photo=has_photo,
    )

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        rehearsal = await asyncio.to_thread(_rehearse, inbound)
    except Exception:
        log.exception("Rehearsal failed")
        await message.reply_text("Something went wrong running that. Check the logs.")
        return

    await message.reply_text(
        _format_rehearsal(rehearsal),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start`. In a DM from the admin, this is how the bot learns to reach them."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != ChatType.PRIVATE:
        return

    if settings.admin_telegram_user_id is not None:
        is_admin = user is not None and user.id == settings.admin_telegram_user_id
    else:
        # Nobody configured: first /start takes the seat.
        is_admin = await asyncio.to_thread(_claim_admin, chat.id) or await asyncio.to_thread(
            _is_admin_chat, chat.id, user.id if user else None
        )

    if is_admin:
        await asyncio.to_thread(_save_admin_chat, chat.id)
        await update.effective_message.reply_text(
            "Connected. I'll message you here whenever a question reaches the queue.\n\n"
            "Send me any message and I'll show you what I would do with it in the "
            "group — answer, stay quiet, or ignore — without actually doing it. "
            "Good way to test before adding me anywhere.\n\n"
            "/status — what's waiting\n"
            "/diag — check I can see group messages"
        )
        log.info("Admin chat registered: %s", chat.id)
    else:
        await update.effective_message.reply_text(
            "Hi. I answer support questions in the group — ask there and I'll pick it up."
        )
        if user is not None:
            log.info(
                "/start from user %s, which is not ADMIN_TELEGRAM_USER_ID (%s).",
                user.id,
                settings.admin_telegram_user_id,
            )


def _save_admin_chat(chat_id: int) -> None:
    with session_scope() as session:
        set_admin_chat_id(session, TELEGRAM, chat_id)


def _queue_summary() -> str:
    from sqlalchemy import func, select

    from app.models import UNANSWERED, QueryKeyword

    with session_scope() as session:
        waiting = session.scalar(
            select(func.count(Query.id)).where(
                Query.channel == TELEGRAM, Query.state == UNANSWERED
            )
        )
        answered = session.scalar(
            select(func.count(Query.id)).where(
                Query.channel == TELEGRAM, Query.state != UNANSWERED
            )
        )
        new_terms = list(
            session.scalars(
                select(QueryKeyword.term)
                .where(QueryKeyword.is_new.is_(True))
                .group_by(QueryKeyword.term)
                .order_by(func.count(QueryKeyword.id).desc())
                .limit(8)
            )
        )

    lines = [f"{waiting} waiting · {answered} answered by me"]
    if new_terms:
        lines.append("\nAsked about, not in your knowledge base:")
        lines += [f"  • {t}" for t in new_terms]
    return "\n".join(lines)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.effective_message.reply_text(await asyncio.to_thread(_queue_summary))


async def on_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers the question everyone hits first: can it see group messages?"""
    chat = update.effective_chat
    me = await context.bot.get_me()

    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"@{me.username} is running.\n"
            f"Run /diag inside the group to check privacy mode there."
        )
        return

    with_terms = await asyncio.to_thread(_keyword_count)
    await update.effective_message.reply_text(
        f"I can see messages in this group.\n"
        f"chat id: {chat.id}\n"
        f"target keywords loaded: {with_terms}\n"
        f"If I stay silent on a question containing one of those, check the logs."
    )


def _keyword_count() -> int:
    from sqlalchemy import func, select

    from app.models import Keyword

    with session_scope() as session:
        return session.scalar(
            select(func.count(Keyword.id)).where(Keyword.channel == TELEGRAM)
        )


def _abandon_flows(chat_id: int, user_id: int) -> int:
    from sqlalchemy import select

    from app.models import ABANDONED, ACTIVE, Conversation

    with session_scope() as session:
        rows = session.scalars(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.user_id == user_id,
                Conversation.state == ACTIVE,
            )
        ).all()
        for convo in rows:
            convo.state = ABANDONED
        return len(rows)


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cancel`. A way out of a guided flow that has gone somewhere unhelpful."""
    chat = update.effective_chat
    user = update.effective_user
    if user is None:
        return

    stopped = await asyncio.to_thread(_abandon_flows, chat.id, user.id)
    await update.effective_message.reply_text(
        "Stopped — ask me anything else." if stopped else "Nothing to cancel."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error in a handler", exc_info=context.error)


def build_application() -> Application:
    init_db()
    app = Application.builder().token(settings.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("diag", on_diag))
    app.add_handler(CommandHandler("cancel", on_cancel))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            & ~filters.COMMAND
            & filters.ChatType.GROUPS,
            on_group_message,
        )
    )
    # Rehearsal in a DM, so the bot can be tried before it joins a group.
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            on_private_message,
        )
    )
    app.add_error_handler(on_error)
    return app


def run() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    gaps = settings.missing()
    if gaps:
        raise SystemExit(
            "Cannot start — these are not set in bot/.env: " + ", ".join(gaps)
        )

    log.info("Starting on model %s. Polling.", settings.groq_model)
    build_application().run_polling(
        # Skip anything that piled up while the bot was down; answering an hour
        # late is worse than not answering.
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    run()
