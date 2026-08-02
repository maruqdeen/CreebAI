"""The escalation DM.

This is the layer that cannot be exercised against a live bot here, so the
message body, its escaping, and the no-admin-registered path are covered.
"""

import pytest
from sqlalchemy import select

from app.groq_client import Verdict
from app.models import Query
from app.notify import (
    build_escalation_message,
    get_admin_chat_id,
    notify_escalation,
    set_admin_chat_id,
)
from app.pipeline import QUEUE, Decision, Inbound, handle


def queued(session, judge, text: str, **verdict_kw) -> tuple[Query, Decision]:
    """Push a message through the pipeline so it lands in the queue."""
    if verdict_kw:
        judge.verdict = Verdict(**verdict_kw)
    inbound = Inbound(
        text=text, channel="telegram", chat_id=-100, message_id=7,
        user_id=555, user_handle="@adaeze_k",
    )
    decision = handle(inbound, session, judge)
    assert decision.action == QUEUE
    return session.scalar(select(Query)), decision


class FakeBot:
    """Records send_message calls instead of hitting Telegram."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    async def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append(kwargs)


# --- Message body ----------------------------------------------------------


def test_message_carries_ref_asker_question_and_reason(session, judge):
    query, decision = queued(session, judge, "how long does the KYC review take?")
    body = build_escalation_message(query, decision)

    assert query.ref in body
    assert "@adaeze_k" in body
    assert "KYC review" in body
    assert "keyword discovery" in body


def test_message_lists_only_the_genuinely_new_keywords(session, judge):
    query, decision = queued(session, judge, "how long does the KYC review take?")
    body = build_escalation_message(query, decision)

    assert "<code>kyc</code>" in body
    assert "Not in your knowledge base yet" in body


def test_message_includes_the_draft_when_the_model_produced_one(session, judge):
    query, decision = queued(
        session,
        judge,
        "i want a refund on my payout",
        answer="I can look into that refund.",
        confidence=0.9,
        covered_by_kb=True,
        topic="refund",
        keywords=["refund"],
    )
    body = build_escalation_message(query, decision)

    assert "Draft ready in the panel" in body
    assert "I can look into that refund." in body
    assert "confidence 0.90" in body


def test_silent_capture_has_no_draft_section(session, judge):
    """No model call means no draft — Redraft in the panel fills it later."""
    query, decision = queued(session, judge, "how long does the KYC review take?")
    assert "Draft ready" not in build_escalation_message(query, decision)


# These two carry the "payout" keyword, so they reach the model. The verdict
# below is what sends them to the queue rather than into a group reply.
ESCALATING = dict(
    answer="", confidence=0.1, covered_by_kb=False, topic="payout", keywords=[]
)


def test_user_supplied_html_is_escaped(session, judge):
    """A user typing markup must never become markup in the admin's DM."""
    query, decision = queued(
        session,
        judge,
        "is my <b>payout</b> late? <script>alert(1)</script>",
        **ESCALATING,
    )
    body = build_escalation_message(query, decision)

    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&lt;b&gt;payout&lt;/b&gt;" in body


def test_long_questions_are_truncated(session, judge):
    query, _ = queued(session, judge, "payout " + "x" * 2000 + "?", **ESCALATING)
    body = build_escalation_message(query, Decision(QUEUE, "too long"))
    assert "…" in body
    assert len(body) < 2000


# --- Delivery --------------------------------------------------------------


async def test_escalation_is_sent_to_the_registered_admin(session, judge):
    query, decision = queued(session, judge, "how long does the KYC review take?")
    bot = FakeBot()

    assert await notify_escalation(bot, query, decision, admin_chat_id=4242) is True
    assert bot.sent[0]["chat_id"] == 4242
    assert query.ref in bot.sent[0]["text"]


async def test_no_registered_admin_reports_false_without_raising(session, judge, caplog):
    """The query is still queued; only the notification is missed."""
    query, decision = queued(session, judge, "how long does the KYC review take?")

    assert await notify_escalation(FakeBot(), query, decision, admin_chat_id=None) is False
    assert "/start" in caplog.text, "the log must say how to fix it"


async def test_a_failed_send_does_not_propagate(session, judge):
    """A blocked bot must not take down the message handler."""
    query, decision = queued(session, judge, "how long does the KYC review take?")
    assert await notify_escalation(FakeBot(fail=True), query, decision, 4242) is False


# --- Admin chat registration ----------------------------------------------


def test_admin_chat_id_round_trips(session):
    assert get_admin_chat_id(session, "telegram") is None
    set_admin_chat_id(session, "telegram", 987654)
    session.flush()
    assert get_admin_chat_id(session, "telegram") == 987654
