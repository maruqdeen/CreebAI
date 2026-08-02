"""The decision matrix.

Every branch of `decide()` plus what `persist()` writes for each.
"""

import pytest
from sqlalchemy import select

from app.groq_client import Verdict
from app.models import ANSWERED, UNANSWERED, Query, QueryKeyword
from app.pipeline import IGNORE, QUEUE, REPLY, Inbound, decide, handle


def msg(text: str, **kw) -> Inbound:
    return Inbound(
        text=text,
        channel="telegram",
        chat_id=-100123,
        message_id=kw.pop("message_id", 1),
        user_id=555,
        user_handle="@adaeze_k",
        **kw,
    )


# --- Ignored ---------------------------------------------------------------


@pytest.mark.parametrize(
    "inbound, why",
    [
        (msg("when is the payout processed?", from_bot=True), "from a bot"),
        (msg("/start"), "a command"),
        (msg("   "), "empty"),
        (msg("lol same"), "chatter: no keyword, not a question"),
        (msg("gm everyone"), "chatter"),
    ],
)
def test_ignored_messages_are_dropped(inbound, why, session, judge):
    decision = handle(inbound, session, judge)
    assert decision.action == IGNORE, why
    assert not judge.called, "an ignored message must never reach the model"
    assert session.scalar(select(Query)) is None, "nothing should be stored"


# --- The answer path -------------------------------------------------------


def test_covered_question_gets_answered_in_group(session, judge):
    decision = handle(msg("when is the payout processed?"), session, judge)

    assert decision.action == REPLY
    assert decision.reply_text == "Payouts run every Tuesday and Friday."
    assert decision.matched_keywords == ["payout"]
    assert judge.called

    query = session.scalar(select(Query))
    assert query.state == ANSWERED
    assert query.answered_by == "bot"
    assert query.ref == "TG-0001"
    assert query.answer_text == "Payouts run every Tuesday and Friday."
    assert query.answered_at is not None
    assert query.latency_ms == 120


def test_refs_increment_per_channel(session, judge):
    handle(msg("when is the payout processed?", message_id=1), session, judge)
    handle(msg("payout question two?", message_id=2), session, judge)
    refs = list(session.scalars(select(Query.ref).order_by(Query.id)))
    assert refs == ["TG-0001", "TG-0002"]


# --- Escalation ------------------------------------------------------------


def test_low_confidence_stays_quiet_and_queues(session, judge):
    judge.verdict = Verdict(
        answer="Probably Tuesday.",
        confidence=0.40,
        covered_by_kb=True,
        topic="payout timing",
        keywords=["payout"],
    )
    decision = handle(msg("is the payout late this week?"), session, judge)

    assert decision.action == QUEUE
    assert "0.40" in decision.reason and "0.62" in decision.reason
    assert decision.reply_text is None

    query = session.scalar(select(Query))
    assert query.state == UNANSWERED
    # The model's text is kept as a draft for the composer, never sent.
    assert query.draft == "Probably Tuesday."
    assert query.answer_text is None
    assert query.confidence == 0.40


def test_not_covered_by_kb_queues(session, judge):
    judge.verdict = Verdict(
        answer="",
        confidence=0.95,
        covered_by_kb=False,
        topic="payout to crypto wallet",
        keywords=["crypto", "wallet"],
    )
    decision = handle(msg("can i take a payout to a crypto wallet?"), session, judge)

    assert decision.action == QUEUE
    assert "No knowledge base entry covers" in decision.reason
    # High confidence must not rescue an uncovered question.
    assert session.scalar(select(Query)).state == UNANSWERED


def test_rule_trigger_overrides_a_confident_answer(session, judge):
    judge.verdict = Verdict(
        answer="Sure, I've refunded you.",
        confidence=0.99,
        covered_by_kb=True,
        topic="refund",
        keywords=["refund"],
    )
    decision = handle(msg("i want a refund for my last payout"), session, judge)

    assert decision.action == QUEUE
    assert decision.rule_ref == "R-04"
    assert "Held by rule R-04" in decision.reason
    assert session.scalar(select(Query)).answer_text is None


def test_complaint_rule_fires_on_a_keyworded_message(session, judge):
    decision = handle(msg("this payout delay is unacceptable"), session, judge)
    assert decision.action == QUEUE
    assert decision.rule_ref == "R-01"


def test_advisory_rule_without_triggers_never_blocks(session, judge):
    """R-02 has no triggers: it shapes the prompt but must not hold anything."""
    decision = handle(msg("when is the payout processed?"), session, judge)
    assert decision.action == REPLY
    assert decision.rule_ref is None
    _, _, rules = judge.calls[0]
    assert any(r.startswith("R-02") for r in rules), "advisory rule still reaches the prompt"


def test_model_failure_fails_closed(session, judge):
    judge.verdict = Verdict.failed("APIConnectionError: connection refused")
    decision = handle(msg("when is the payout processed?"), session, judge)

    assert decision.action == QUEUE, "a failed model call must never reply"
    assert "Model call failed" in decision.reason
    assert session.scalar(select(Query)).state == UNANSWERED


def test_empty_answer_queues_even_when_covered(session, judge):
    judge.verdict = Verdict(
        answer="", confidence=0.9, covered_by_kb=True, topic="payout", keywords=[]
    )
    assert handle(msg("payout?"), session, judge).action == QUEUE


# --- Silent capture: the keyword-discovery path ----------------------------


def test_unknown_topic_question_is_captured_without_a_model_call(session, judge):
    decision = handle(msg("how long does the KYC review take?"), session, judge)

    assert decision.action == QUEUE
    assert not judge.called, "silent capture must cost nothing"
    assert "keyword discovery" in decision.reason

    query = session.scalar(select(Query))
    assert query.state == UNANSWERED
    assert query.draft is None, "no model call means no draft; Redraft fills it later"
    assert query.confidence is None


def test_capture_flags_terms_absent_from_keywords_and_kb(session, judge):
    handle(msg("how long does the KYC review take?"), session, judge)

    new_terms = set(
        session.scalars(select(QueryKeyword.term).where(QueryKeyword.is_new.is_(True)))
    )
    assert "kyc" in new_terms
    assert "review" in new_terms


def test_terms_already_in_the_kb_are_not_flagged_new(session, judge):
    """A word the knowledge base already uses is not a discovery.

    Goes down the capture path on purpose, since that is where `is_new` is
    computed against the knowledge base corpus.
    """
    handle(msg("is tuesday a sensible choice for this?"), session, judge)

    rows = {
        term: is_new
        for term, is_new in session.execute(
            select(QueryKeyword.term, QueryKeyword.is_new)
        ).all()
    }
    assert rows["tuesday"] is False, "'tuesday' appears in the seeded KB text"
    assert rows["sensible"] is True, "an unrelated word is a genuine discovery"


def test_target_keywords_are_never_flagged_new(session, judge):
    handle(msg("when is the payout processed?"), session, judge)
    rows = {
        term: is_new
        for term, is_new in session.execute(
            select(QueryKeyword.term, QueryKeyword.is_new)
        ).all()
    }
    assert rows["payout"] is False


def test_complaint_without_a_keyword_is_still_captured(session, judge):
    decision = handle(msg("this app is a scam and i am furious"), session, judge)
    assert decision.action == QUEUE
    assert not judge.called
    assert session.scalar(select(Query)).state == UNANSWERED


def test_statement_without_keyword_or_question_shape_is_dropped(session, judge):
    decision = handle(msg("thanks everyone for the help earlier"), session, judge)
    assert decision.action == IGNORE
    assert session.scalar(select(Query)) is None
