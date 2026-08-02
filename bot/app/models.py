"""Database tables.

Shaped from what the admin panel in ../support-desk already displays, so the
panel needs no reshaping when it is wired up — and so Laravel can later read
these same tables directly.

Dashboard figures (totals, per-channel splits, median reply, most-asked with
ask counts) are SQL aggregates over `queries` and `query_keywords`. There are
deliberately no rollup tables to keep in sync.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TELEGRAM = "telegram"
EMAIL = "email"
CHANNELS = (TELEGRAM, EMAIL)

# Query lifecycle
UNANSWERED = "unanswered"
ANSWERED = "answered"

# Who produced the answer
BY_BOT = "bot"
BY_ADMIN = "admin"

# Conversation lifecycle
ACTIVE = "active"
FINISHED = "finished"
ABANDONED = "abandoned"

REF_PREFIX = {TELEGRAM: "TG", EMAIL: "EM"}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ChannelSettings(Base):
    """One row per channel. Mirrors a single tab of the Settings page."""

    __tablename__ = "channel_settings"
    __table_args__ = (
        CheckConstraint(f"channel IN {CHANNELS!r}", name="ck_channel_settings_channel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), unique=True)

    # Connection
    bot_link: Mapped[str | None] = mapped_column(String(255))
    group_chat_id: Mapped[int | None] = mapped_column(Integer)
    # Learned when the admin /starts the bot in a DM. A bot cannot message a
    # user who has never messaged it, so this stays null until they do.
    admin_chat_id: Mapped[int | None] = mapped_column(Integer)

    # Behaviour
    kb_text: Mapped[str] = mapped_column(Text, default="")
    reply_threshold: Mapped[float] = mapped_column(Float, default=0.62)

    #: What makes a message ours at all. A group message that mentions none of
    #: these is other people talking to each other, and the bot never reads it,
    #: answers it, captures it, or reports it.
    #:
    #: Distinct from target keywords: those are subjects we can answer *about*
    #: the product. This is "is this even about the product". Comma-separated;
    #: empty means no restriction.
    product_terms: Mapped[str] = mapped_column(Text, default="")

    def product_term_list(self) -> list[str]:
        return [t.strip().lower() for t in (self.product_terms or "").split(",") if t.strip()]

    # Digest (scheduler itself is a later phase; the settings live here now)
    digest_to: Mapped[str | None] = mapped_column(String(255))
    digest_weekly: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_monthly: Mapped[bool] = mapped_column(Boolean, default=True)

    # Monotonic per-channel counter behind the TG-#### / EM-#### refs.
    ref_counter: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Keyword(Base):
    """Target keywords. A group message must contain one to reach the model."""

    __tablename__ = "keywords"
    __table_args__ = (UniqueConstraint("channel", "term", name="uq_keyword_per_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    term: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Rule(Base):
    """Hard limits. Applied after the model and always beating it."""

    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("channel", "ref", name="uq_rule_ref_per_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    ref: Mapped[str] = mapped_column(String(8))  # R-01, R-02 …
    text: Mapped[str] = mapped_column(Text)
    # Optional trigger terms. When present, a message containing one of these
    # is held for a human regardless of what the model decided. When absent the
    # rule is advisory only: it is injected into the prompt but enforces nothing.
    triggers: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def trigger_terms(self) -> list[str]:
        return [t.strip().lower() for t in self.triggers.split(",") if t.strip()]


class KBEntry(Base):
    """A discrete knowledge base entry.

    `source='filed'` rows are written by the composer's *File to the knowledge
    base* step — the loop that shrinks the queue.
    """

    __tablename__ = "kb_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | filed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Query(Base):
    """One inbound user message the bot took an interest in.

    Messages that match no keyword and do not read as a question are never
    stored — they are ordinary group chatter and none of our business.
    """

    __tablename__ = "queries"
    __table_args__ = (
        UniqueConstraint("channel", "ref", name="uq_query_ref_per_channel"),
        Index("ix_queries_state_received", "channel", "state", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(16), index=True)

    # Telegram provenance, enough to reply in-thread later
    tg_chat_id: Mapped[int | None] = mapped_column(Integer)
    tg_message_id: Mapped[int | None] = mapped_column(Integer)
    tg_user_id: Mapped[int | None] = mapped_column(Integer)
    user_handle: Mapped[str | None] = mapped_column(String(128))

    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    state: Mapped[str] = mapped_column(String(16), default=UNANSWERED, index=True)
    answered_by: Mapped[str | None] = mapped_column(String(16))

    # The model's verdict, kept whether or not we acted on it — this is what
    # makes threshold tuning possible after the fact.
    confidence: Mapped[float | None] = mapped_column(Float)
    covered_by_kb: Mapped[bool | None] = mapped_column(Boolean)
    topic: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)

    draft: Mapped[str | None] = mapped_column(Text)
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    filed_kb_entry_id: Mapped[int | None] = mapped_column(ForeignKey("kb_entries.id"))

    #: Set when the query came out of a guided flow — the questions the bot
    #: asked and the answers given, so the admin opens the row already knowing
    #: the region, the app version and what was tried.
    transcript: Mapped[str | None] = mapped_column(Text)

    keywords: Mapped[list["QueryKeyword"]] = relationship(
        back_populates="query", cascade="all, delete-orphan", lazy="selectin"
    )


class Conversation(Base):
    """Where one person has got to in a guided flow.

    Keyed on (channel, chat_id, user_id) so two people in the same group can be
    mid-flow at once without colliding. Rows are kept after they finish: they
    are the record of what was asked and answered.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversation_party", "channel", "chat_id", "user_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    chat_id: Mapped[int | None] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(Integer)

    flow_id: Mapped[str] = mapped_column(String(64))
    step_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default=ACTIVE)

    #: JSON list of {role, text, photo} — see flows.render_transcript.
    transcript: Mapped[str] = mapped_column(Text, default="[]")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def entries(self) -> list[dict]:
        import json

        try:
            return json.loads(self.transcript or "[]")
        except ValueError:
            return []

    def append(self, role: str, text: str, photo: bool = False) -> None:
        import json

        entries = self.entries()
        entries.append({"role": role, "text": text or "", "photo": photo})
        self.transcript = json.dumps(entries)


class QueryKeyword(Base):
    """Terms a query introduced.

    `is_new` means the term is in neither the target-keyword list nor any
    knowledge base entry. Filtering this table on `is_new` *is* the "new
    keywords asked that the knowledge base does not answer" report.
    """

    __tablename__ = "query_keywords"
    __table_args__ = (
        UniqueConstraint("query_id", "term", name="uq_keyword_per_query"),
        Index("ix_query_keywords_new", "is_new", "term"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("queries.id", ondelete="CASCADE"))
    term: Mapped[str] = mapped_column(String(64))
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)

    query: Mapped[Query] = relationship(back_populates="keywords")
