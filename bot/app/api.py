"""HTTP API for the admin panel.

This is the boundary the panel talks to now and Laravel will talk to later —
the shapes in schemas.py match what ../support-desk already renders.

Runs in the same process as the bot (see main.py) so replies sent from the
panel go out over the same Telegram connection.
"""

import logging
import statistics
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi import Query as Q
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, init_db
from app.groq_client import GroqJudge
from app.kb import derive_keywords
from app.models import (
    ANSWERED,
    BY_ADMIN,
    CHANNELS,
    TELEGRAM,
    UNANSWERED,
    ChannelSettings,
    KBEntry,
    Keyword,
    Query,
    QueryKeyword,
    Rule,
    utcnow,
)
from app.pipeline import ChannelContext
from app.security import (
    TOKEN_TTL_SECONDS,
    admin_configured,
    authenticate,
    read_token,
)
from app.webhook import is_registered, telegram_router
from app.schemas import (
    AskedItem,
    ChannelTotals,
    DashboardOut,
    KeywordCount,
    QueryOut,
    RedraftOut,
    ReplyIn,
    ReplyOut,
    RuleIn,
    LoginIn,
    LoginOut,
    SettingsIn,
    SettingsOut,
    SuggestIn,
)

log = logging.getLogger(__name__)

PERIODS = {"week": 7, "month": 30}

#: The `keywords.term` column width. A gate term is something that appears
#: inside what a user types, so anything near this length is almost certainly a
#: whole question pasted into the wrong field.
MAX_KEYWORD_LEN = 64


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def require_admin(request: Request) -> str:
    """Gate for everything that reads user messages or changes bot behaviour.

    Without this the deployed panel would let anyone with the URL read the
    queue and rewrite the knowledge base the bot answers from.
    """
    subject = read_token(_bearer(request) or "")
    if subject is None:
        raise HTTPException(
            401,
            "Not signed in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return subject


# Everything under /api requires a session except the two below: `health` so a
# host can probe the service, and `auth/login` so a session can be obtained at
# all. The Telegram webhook sits outside /api and carries its own secret.
router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
public = APIRouter(prefix="/api")


@public.post("/auth/login", response_model=LoginOut)
def login(payload: LoginIn):
    """Exchange a username and password for a session token."""
    token = authenticate(payload.username, payload.password)
    if token is None:
        if not admin_configured():
            raise HTTPException(
                503,
                "No administrator is configured on this deployment. Set "
                "ADMIN_USERNAME and ADMIN_PASSWORD_HASH.",
            )
        # One message for both failures: never reveal which half was wrong.
        raise HTTPException(401, "Wrong username or password.")
    return LoginOut(token=token, username=payload.username, expires_in=TOKEN_TTL_SECONDS)


@public.get("/auth/me")
def whoami(request: Request):
    """Whether the token in hand is still good — the panel's session check."""
    subject = read_token(_bearer(request) or "")
    if subject is None:
        raise HTTPException(401, "Not signed in.")
    return {"username": subject}


def _since(period: str) -> datetime:
    return datetime.now(UTC) - timedelta(days=PERIODS.get(period, 7))


def _settings_row(session: Session, channel: str) -> ChannelSettings:
    cs = session.scalar(select(ChannelSettings).where(ChannelSettings.channel == channel))
    if cs is None:
        raise HTTPException(404, f"No settings for channel {channel!r}")
    return cs


# --- Dashboard -------------------------------------------------------------


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(
    period: str = Q("week", pattern="^(week|month)$"),
    session: Session = Depends(get_session),
):
    since = _since(period)
    channels: list[ChannelTotals] = []
    total_answered = 0

    for channel in CHANNELS:
        answered = session.scalar(
            select(func.count(Query.id)).where(
                Query.channel == channel, Query.state == ANSWERED, Query.received_at >= since
            )
        )
        unanswered = session.scalar(
            select(func.count(Query.id)).where(
                Query.channel == channel, Query.state == UNANSWERED
            )
        )
        latencies = list(
            session.scalars(
                select(Query.latency_ms).where(
                    Query.channel == channel,
                    Query.latency_ms.is_not(None),
                    Query.received_at >= since,
                )
            )
        )
        channels.append(
            ChannelTotals(
                channel=channel,
                answered=answered or 0,
                unanswered=unanswered or 0,
                median_reply_ms=int(statistics.median(latencies)) if latencies else None,
            )
        )
        total_answered += answered or 0

    # Group by the model's topic where there is one, otherwise the question
    # itself — silent-capture rows never got a topic and still belong here.
    label = func.coalesce(func.nullif(Query.topic, ""), func.substr(Query.body, 1, 90))
    most_asked: dict[str, list[AskedItem]] = {}
    for channel in CHANNELS:
        rows = session.execute(
            select(
                label,
                func.count(Query.id),
                func.max(func.iif(Query.state == ANSWERED, 1, 0)),
            )
            .where(Query.channel == channel, Query.received_at >= since)
            .group_by(label)
            .order_by(func.count(Query.id).desc())
            .limit(5)
        ).all()
        most_asked[channel] = [
            AskedItem(question=text, count=count, in_kb=bool(answered))
            for text, count, answered in rows
        ]

    new_rows = session.execute(
        select(QueryKeyword.term, func.count(QueryKeyword.id))
        .join(Query, Query.id == QueryKeyword.query_id)
        .where(QueryKeyword.is_new.is_(True), Query.received_at >= since)
        .group_by(QueryKeyword.term)
        .order_by(func.count(QueryKeyword.id).desc())
        .limit(20)
    ).all()

    return DashboardOut(
        period=period,
        since=since,
        total_answered=total_answered,
        channels=channels,
        most_asked=most_asked,
        new_keywords=[KeywordCount(term=t, count=c, is_new=True) for t, c in new_rows],
    )


# --- Queue -----------------------------------------------------------------


@router.get("/queries", response_model=list[QueryOut])
def list_queries(
    channel: str = Q(TELEGRAM),
    state: str | None = Q(None, pattern="^(unanswered|answered)$"),
    limit: int = Q(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    stmt = select(Query).where(Query.channel == channel)
    if state:
        stmt = stmt.where(Query.state == state)
    stmt = stmt.order_by(Query.received_at.desc()).limit(limit)
    return [QueryOut.of(q) for q in session.scalars(stmt)]


@router.get("/queries/{ref}", response_model=QueryOut)
def get_query(ref: str, session: Session = Depends(get_session)):
    query = session.scalar(select(Query).where(Query.ref == ref))
    if query is None:
        raise HTTPException(404, f"No query {ref!r}")
    return QueryOut.of(query)


@router.post("/queries/{ref}/reply", response_model=ReplyOut)
async def reply(ref: str, payload: ReplyIn, session: Session = Depends(get_session)):
    """Send the operator's reply, and optionally file it to the knowledge base.

    The knowledge base entry is written even when Telegram delivery fails —
    what was learned is worth keeping regardless of whether the message landed,
    and `delivery_error` tells the panel to report the failure honestly rather
    than showing a success it did not get.
    """
    query = session.scalar(select(Query).where(Query.ref == ref))
    if query is None:
        raise HTTPException(404, f"No query {ref!r}")
    if query.state == ANSWERED:
        raise HTTPException(409, f"{ref} has already been answered")

    delivered, error = False, None
    if query.channel == TELEGRAM and query.tg_chat_id:
        if not settings.telegram_configured:
            error = "TELEGRAM_BOT_TOKEN is not set"
        else:
            try:
                from telegram import Bot

                from app.telegram_bot import split_for_telegram

                bot = Bot(settings.telegram_bot_token)
                # Split rather than truncate: an operator's reply that quotes a
                # procedure must not lose its last steps on the way out.
                for index, part in enumerate(split_for_telegram(payload.text)):
                    await bot.send_message(
                        chat_id=query.tg_chat_id,
                        text=part,
                        reply_to_message_id=query.tg_message_id if index == 0 else None,
                        disable_web_page_preview=True,
                    )
                delivered = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("Could not deliver the reply for %s: %s", ref, error)
    else:
        error = f"No delivery route for channel {query.channel!r}"

    entry_id = None
    promoted: list[str] = []
    if payload.file_to_kb:
        entry = KBEntry(
            channel=query.channel,
            title=(payload.kb_title or query.topic or query.body[:80]).strip(),
            body=payload.text,
            source="filed",
        )
        session.add(entry)
        session.flush()
        entry_id = entry.id
        query.filed_kb_entry_id = entry_id

        # Filing the answer is only half of it. Without promoting the terms
        # that made this a discovery, the keyword gate keeps sending the same
        # question to the queue and "the bot answers this itself next time" is
        # not true. Promoted terms are ordinary keywords — removable from the
        # settings page like any other.
        promoted = _promote_keywords(session, query)

    query.state = ANSWERED
    query.answered_by = BY_ADMIN
    query.answer_text = payload.text
    query.answered_at = utcnow()
    session.commit()

    return ReplyOut(
        ref=ref,
        state=query.state,
        delivered=delivered,
        delivery_error=error,
        filed_kb_entry_id=entry_id,
        promoted_keywords=promoted,
    )


#: Cap on how far filing one answer may widen the gate. A handful of terms is a
#: fix; twenty is an accident that makes the bot talk over the group.
MAX_PROMOTED_PER_REPLY = 5


def _promote_keywords(session: Session, query: Query) -> list[str]:
    """Turn a query's discovered terms into target keywords for its channel."""
    existing = {
        t.lower()
        for t in session.scalars(
            select(Keyword.term).where(Keyword.channel == query.channel)
        )
    }
    promoted: list[str] = []
    for kw in query.keywords:
        if not kw.is_new or kw.term in existing:
            continue
        session.add(Keyword(channel=query.channel, term=kw.term))
        existing.add(kw.term)
        promoted.append(kw.term)
        if len(promoted) >= MAX_PROMOTED_PER_REPLY:
            break
    return promoted


@router.post("/queries/{ref}/redraft", response_model=RedraftOut)
def redraft(ref: str, session: Session = Depends(get_session)):
    """Re-run the model for a fresh draft.

    Also the path that gives silent-capture rows their first draft — those are
    stored without one on purpose, so discovery stays free.
    """
    query = session.scalar(select(Query).where(Query.ref == ref))
    if query is None:
        raise HTTPException(404, f"No query {ref!r}")

    ctx = ChannelContext.load(session, query.channel)
    verdict = GroqJudge().judge(query.body, ctx.prompt_kb(), ctx.rule_texts())

    if verdict.ok:
        query.draft = verdict.answer
        query.confidence = verdict.confidence
        query.covered_by_kb = verdict.covered_by_kb
        query.topic = query.topic or (verdict.topic or None)
        session.commit()

    return RedraftOut(
        ref=ref,
        draft=verdict.answer,
        confidence=verdict.confidence,
        covered_by_kb=verdict.covered_by_kb,
        error=verdict.error,
    )


# --- Keywords --------------------------------------------------------------


@router.get("/keywords/new", response_model=list[KeywordCount])
def new_keywords(
    limit: int = Q(50, ge=1, le=200), session: Session = Depends(get_session)
):
    """Terms users asked about that neither the keyword list nor the KB covers."""
    rows = session.execute(
        select(QueryKeyword.term, func.count(QueryKeyword.id))
        .where(QueryKeyword.is_new.is_(True))
        .group_by(QueryKeyword.term)
        .order_by(func.count(QueryKeyword.id).desc())
        .limit(limit)
    ).all()
    return [KeywordCount(term=t, count=c, is_new=True) for t, c in rows]


# --- Settings --------------------------------------------------------------


@router.post("/settings/{channel}/keywords/suggest", response_model=list[KeywordCount])
def suggest_keywords(
    channel: str, payload: SuggestIn, session: Session = Depends(get_session)
):
    """Derive target keywords from knowledge base text. Saves nothing.

    The gate and the knowledge base have to agree or the bot goes mute on its
    own subject matter — a knowledge base about VPNs is never consulted while
    the keywords still say "payout". This lets the panel show suggestions
    beside the textarea before anything is committed.
    """
    text = payload.kb_text
    if text is None:
        text = _settings_row(session, channel).kb_text or ""

    existing = list(
        session.scalars(select(Keyword.term).where(Keyword.channel == channel))
    )
    keep = existing if payload.exclude_existing else []
    return [
        KeywordCount(term=term, count=count, is_new=term not in existing)
        for term, count in derive_keywords(text, limit=payload.limit, exclude=keep)
    ]


@router.get("/settings/{channel}", response_model=SettingsOut)
def get_settings(channel: str, session: Session = Depends(get_session)):
    cs = _settings_row(session, channel)
    return SettingsOut(
        channel=cs.channel,
        bot_link=cs.bot_link,
        group_chat_id=cs.group_chat_id,
        admin_chat_id=cs.admin_chat_id,
        kb_text=cs.kb_text or "",
        reply_threshold=cs.reply_threshold,
        digest_to=cs.digest_to,
        digest_weekly=cs.digest_weekly,
        digest_monthly=cs.digest_monthly,
        product_terms=cs.product_term_list(),
        keywords=list(
            session.scalars(
                select(Keyword.term).where(Keyword.channel == channel).order_by(Keyword.term)
            )
        ),
        rules=[
            RuleIn(ref=r.ref, text=r.text, triggers=r.triggers, active=r.active)
            for r in session.scalars(
                select(Rule).where(Rule.channel == channel).order_by(Rule.position, Rule.ref)
            )
        ],
    )


@router.put("/settings/{channel}", response_model=SettingsOut)
def update_settings(
    channel: str, payload: SettingsIn, session: Session = Depends(get_session)
):
    cs = _settings_row(session, channel)

    for field in ("kb_text", "reply_threshold", "bot_link", "digest_to",
                  "digest_weekly", "digest_monthly"):
        value = getattr(payload, field)
        if value is not None:
            setattr(cs, field, value)

    if payload.product_terms is not None:
        # Stored as one field rather than a table: it is a short scope list the
        # operator edits as a whole, not a growing collection.
        cleaned = []
        for term in payload.product_terms:
            term = term.strip().lower()
            if term and term not in cleaned:
                cleaned.append(term)
        cs.product_terms = ", ".join(cleaned)

    if payload.keywords is not None:
        # Validate before deleting anything: a rejected save must not leave the
        # operator with an empty keyword list.
        too_long = [t.strip() for t in payload.keywords if len(t.strip()) > MAX_KEYWORD_LEN]
        if too_long:
            raise HTTPException(
                422,
                f"{len(too_long)} keyword(s) are longer than {MAX_KEYWORD_LEN} "
                f"characters, starting with “{too_long[0][:70]}…”. Keywords are "
                f"the words a user types — “add time”, “dns”, “mtn” — not whole "
                f"questions. Whole questions belong in the knowledge base, where "
                f"the model reads them.",
            )

        # Replace wholesale: the settings page edits the list as a whole.
        session.query(Keyword).filter(Keyword.channel == channel).delete()
        seen: set[str] = set()
        for term in payload.keywords:
            term = term.strip().lower()
            if term and term not in seen:
                seen.add(term)
                session.add(Keyword(channel=channel, term=term))

    if payload.rules is not None:
        session.query(Rule).filter(Rule.channel == channel).delete()
        for position, rule in enumerate(payload.rules):
            session.add(
                Rule(
                    channel=channel,
                    ref=rule.ref,
                    text=rule.text,
                    triggers=rule.triggers,
                    active=rule.active,
                    position=position,
                )
            )

    session.commit()
    return get_settings(channel, session)


# --- App -------------------------------------------------------------------


@public.get("/health")
def health():
    """Unauthenticated on purpose: hosts probe this to decide if we are alive.

    It reports only whether things are configured, never what they are set to.
    """
    return {
        "ok": True,
        "model": settings.groq_model,
        "groq_configured": settings.groq_configured,
        "telegram_configured": settings.telegram_configured,
        "admin_configured": admin_configured(),
        "mode": "webhook" if settings.use_webhook else "polling",
        # False here while mode is "webhook" means the service is up but the
        # bot is receiving nothing — the failure that otherwise looks healthy.
        "webhook_registered": is_registered() if settings.use_webhook else None,
        "missing_settings": settings.missing(),
    }


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="Support Desk API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        # The panel is a separate origin in every environment: Vite locally,
        # a static site in production.
        allow_origins=settings.cors_origins(),
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(public)
    app.include_router(router)
    app.include_router(telegram_router)

    for gap in settings.deployment_gaps():
        log.warning("Not ready to expose publicly — %s", gap)

    return app


app = create_app()
