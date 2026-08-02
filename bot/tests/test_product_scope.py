"""The product-scope gate.

A support group is still a group: most of what is said in it is people talking
to each other. Without this gate, generic target keywords — "time", "data",
"free", "connection" — drag ordinary conversation into the bot, and every
question-shaped message gets captured and reported to the operator.
"""

import pytest
from sqlalchemy import select

from app.models import Query
from app.pipeline import IGNORE, QUEUE, REPLY, ChannelContext, Inbound, handle


@pytest.fixture
def scoped(session):
    """The fixture channel, narrowed to Creeb products."""
    ctx = ChannelContext.load(session, "telegram")
    ctx.settings.product_terms = "creeb, creebvpn, creeb vpn, vpn, injector, tunnel"
    session.flush()
    return session


def msg(text: str, **kw) -> Inbound:
    return Inbound(
        text=text,
        channel="telegram",
        chat_id=-100,
        message_id=kw.pop("message_id", 1),
        user_id=kw.pop("user_id", 777),
        user_handle="@someone",
        **kw,
    )


# --- What the operator was drowning in -------------------------------------


@pytest.mark.parametrize(
    "chatter",
    [
        "anybody know where to buy cheap data?",
        "who get iphone 13 for sale?",
        "how far na, wetin dey happen?",
        "is the network down for everyone today?",
        "what time is the match?",
        "please can someone help me with my account number",
    ],
)
def test_group_conversation_is_not_captured_or_reported(scoped, judge, chatter):
    """These are question-shaped and some hit generic keywords. None are ours."""
    decision = handle(msg(chatter), scoped, judge)

    assert decision.action == IGNORE
    assert not judge.called, "off-topic chatter must not cost a model call"
    assert scoped.scalar(select(Query)) is None, "nothing to report"


def test_the_reason_says_why_it_was_dropped(scoped, judge):
    decision = handle(msg("anybody selling data?"), scoped, judge)
    assert "product" in decision.reason.lower()


# --- What must still get through -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "my creeb vpn is not connecting",
        "how do i connect creebvpn",
        "is creeb injector down?",
        "creeb tunnel keeps disconnecting",
        "vpn no dey work",
    ],
)
def test_messages_about_the_product_still_reach_the_bot(scoped, judge, text):
    assert handle(msg(text), scoped, judge).action != IGNORE


def test_a_product_question_with_no_target_keyword_is_still_captured(scoped, judge):
    """Keyword discovery still works — inside the product scope."""
    decision = handle(msg("does creeb work on iphone?"), scoped, judge)

    assert decision.action == QUEUE
    assert not judge.called
    assert scoped.scalar(select(Query)) is not None


def test_a_product_question_the_kb_covers_is_answered(scoped, judge):
    decision = handle(msg("when is the payout processed for creeb?"), scoped, judge)
    assert decision.action == REPLY
    assert judge.called


# --- Mid-flow answers are exempt -------------------------------------------


def test_an_answer_inside_a_flow_does_not_need_a_product_word(scoped, judge):
    """"Nigeria MTN free 50mb" names no product and must still be understood."""
    started = handle(msg("my creeb vpn is not connecting"), scoped, judge)
    assert "country/region" in started.reply_text

    answered = handle(msg("Nigeria/ghana mtn free 50mb"), scoped, judge)
    assert answered.action == REPLY
    assert "latest app and config version" in answered.reply_text


def test_thanks_still_works_without_a_product_word(scoped, judge):
    result = handle(msg("thank you"), scoped, judge)
    assert result.action == REPLY
    assert "You're welcome" in result.reply_text


# --- Configuration ---------------------------------------------------------


def test_no_product_terms_means_no_restriction(session, judge):
    """An existing setup must not go silent just because the gate exists."""
    ctx = ChannelContext.load(session, "telegram")
    assert ctx.settings.product_term_list() == []

    decision = handle(msg("anybody know where to buy cheap data?"), session, judge)
    assert decision.action == QUEUE, "captured, because nothing narrows the scope"


def test_scope_matching_respects_word_boundaries(scoped, judge):
    """"vpn" must not fire on "vpns" being part of another word."""
    ctx = ChannelContext.load(scoped, "telegram")
    assert ctx.in_scope("my creeb vpn is down") is True
    assert ctx.in_scope("i need a new laptop") is False


def test_scope_is_case_and_apostrophe_insensitive(scoped):
    ctx = ChannelContext.load(scoped, "telegram")
    assert ctx.in_scope("CREEB VPN is not working") is True
    assert ctx.in_scope("creeb's vpn is down") is True
