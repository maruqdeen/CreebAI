"""Request and response shapes for the API.

Field names match what the panel in ../support-desk already renders, so wiring
it up is a fetch call rather than a translation layer.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class LoginOut(BaseModel):
    token: str
    username: str
    expires_in: int


class KeywordCount(BaseModel):
    term: str
    count: int
    is_new: bool


class AskedItem(BaseModel):
    """A row in the "most asked" columns."""

    question: str
    count: int
    in_kb: bool


class ChannelTotals(BaseModel):
    channel: str
    answered: int
    unanswered: int
    median_reply_ms: int | None = None


class DashboardOut(BaseModel):
    period: str
    since: datetime
    total_answered: int
    channels: list[ChannelTotals]
    most_asked: dict[str, list[AskedItem]]
    new_keywords: list[KeywordCount]


class QueryOut(BaseModel):
    ref: str
    channel: str
    state: str
    user_handle: str | None
    body: str
    received_at: datetime
    confidence: float | None
    covered_by_kb: bool | None
    topic: str | None
    reason: str | None
    draft: str | None
    answer_text: str | None
    answered_by: str | None
    new_keywords: list[str] = Field(default_factory=list)
    #: Present when the query came out of a guided flow — the questions asked
    #: and the answers given, so the admin does not have to ask again.
    transcript: str | None = None

    @classmethod
    def of(cls, query) -> "QueryOut":
        return cls(
            ref=query.ref,
            channel=query.channel,
            state=query.state,
            user_handle=query.user_handle,
            body=query.body,
            received_at=query.received_at,
            confidence=query.confidence,
            covered_by_kb=query.covered_by_kb,
            topic=query.topic,
            reason=query.reason,
            draft=query.draft,
            answer_text=query.answer_text,
            answered_by=query.answered_by,
            new_keywords=[k.term for k in query.keywords if k.is_new],
            transcript=query.transcript,
        )


class ReplyIn(BaseModel):
    text: str = Field(min_length=1)
    file_to_kb: bool = True
    kb_title: str | None = None


class ReplyOut(BaseModel):
    ref: str
    state: str
    delivered: bool
    #: Set when delivery failed, so the panel can say so rather than claim success.
    delivery_error: str | None = None
    filed_kb_entry_id: int | None = None
    #: Terms promoted to target keywords so this question reaches the bot next
    #: time. The panel should surface these — they widen what the bot answers.
    promoted_keywords: list[str] = Field(default_factory=list)


class RedraftOut(BaseModel):
    ref: str
    draft: str
    confidence: float
    covered_by_kb: bool
    error: str | None = None


class RuleIn(BaseModel):
    ref: str
    text: str
    triggers: str = ""
    active: bool = True


class SettingsOut(BaseModel):
    channel: str
    bot_link: str | None
    group_chat_id: int | None
    admin_chat_id: int | None
    kb_text: str
    reply_threshold: float
    digest_to: str | None
    digest_weekly: bool
    digest_monthly: bool
    keywords: list[str]
    #: What makes a message the bot's business at all. Empty = no restriction.
    product_terms: list[str]
    rules: list[RuleIn]


class KBImportIn(BaseModel):
    """Paste plain-text knowledge base. No JSON conversion — the model reads
    prose natively; only its *output* verdict is schema-enforced.
    """

    text: str = Field(min_length=1)
    #: Replace the stored knowledge base, or append to what is there.
    replace: bool = True
    #: Derive target keywords from the text and add any new ones automatically.
    add_keywords: bool = True


class KBImportOut(BaseModel):
    kb_text: str
    #: Every candidate keyword derive_keywords() found, with its count — shown
    #: so the operator can see the reasoning, not just the result.
    suggested_keywords: list[KeywordCount]
    #: The subset actually added to the channel's keyword list.
    added_keywords: list[str]


class SuggestIn(BaseModel):
    """Ask for keywords derived from knowledge base text."""

    #: Omit to derive from whatever is already saved for the channel.
    kb_text: str | None = None
    limit: int = Field(default=25, ge=1, le=100)
    #: Leave out terms already configured, so only the additions are shown.
    exclude_existing: bool = False


class SettingsIn(BaseModel):
    kb_text: str | None = None
    reply_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    bot_link: str | None = None
    digest_to: str | None = None
    digest_weekly: bool | None = None
    digest_monthly: bool | None = None
    keywords: list[str] | None = None
    product_terms: list[str] | None = None
    rules: list[RuleIn] | None = None
