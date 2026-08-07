"""Guided diagnostic flows.

The operator wrote a troubleshooting script; the job here is to execute it
exactly, including the parts that make it useful — remembering where someone
is, accepting an answer that carries no keyword, and handing over with
everything already collected.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.flows import load_flows, next_step_id, render_transcript, yes_or_no
from app.groq_client import Verdict
from app.models import ABANDONED, ACTIVE, FINISHED, UNANSWERED, Conversation, Query
from app.pipeline import IGNORE, QUEUE, REPLY, Inbound, handle

FLOWS = load_flows()
FLOW = FLOWS.get("connection-troubleshooting")


def msg(text: str, user_id: int = 555, **kw) -> Inbound:
    return Inbound(
        text=text,
        channel="telegram",
        chat_id=-100,
        message_id=kw.pop("message_id", 1),
        user_id=user_id,
        user_handle=f"@user{user_id}",
        **kw,
    )


# --- Trigger matching ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "my Creeb vpn is not connecting",
        "my vpn is not working",
        "my creeb is not working again",
        "my creeb stop connecting",
        "my creeb stop working",
        "I can't connect to creeb",
        "creeb vpn wont connect",
        "vpn keeps disconnecting",
        # Pidgin — how much of the community actually reports a fault.
        "vpn no dey work",
        "creeb no dey connect",
        "my creeb don stop working",
        "e no dey browse again, creeb",
        "creeb no gree connect",
    ],
)
def test_the_complaints_people_actually_send_start_the_flow(text):
    assert FLOW.matches(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "my phone is not working",  # no product word
        "my data is not working",
        "creeb vpn is great",  # product word, no fault phrase
        "how do i connect creebvpn",  # a question, not a fault report
        "",
    ],
)
def test_unrelated_messages_do_not_start_the_flow(text):
    assert FLOW.matches(text) is False


def test_require_is_what_stops_false_starts():
    """Without the product word the "any" phrases are far too broad."""
    assert "not working" in FLOW.triggers_any
    assert FLOW.triggers_require, "a flow with no require clause would fire constantly"


# --- Yes / no reading ------------------------------------------------------


@pytest.mark.parametrize("text", ["yes", "Yes", "yeah", "yep", "yh", "sure", "i did", "already updated"])
def test_affirmative_answers(text):
    assert yes_or_no(text) == "yes"


@pytest.mark.parametrize("text", ["no", "No", "nope", "nah", "not yet", "i haven't", "didn't"])
def test_negative_answers(text):
    assert yes_or_no(text) == "no"


@pytest.mark.parametrize("text", ["maybe", "what do you mean", "Nigeria MTN 50mb", ""])
def test_unclear_answers_are_neither(text):
    assert yes_or_no(text) is None


def test_unclear_answer_takes_the_fallback_branch():
    """Better to ask for the log than send someone to update what is updated."""
    step = FLOW.step("updated")
    assert next_step_id(step, "maybe") == step.fallback == "screenshot"


def test_a_photo_counts_as_an_answer():
    step = FLOW.step("updated")
    assert next_step_id(step, "", has_photo=True) == "screenshot"


# --- The whole conversation ------------------------------------------------


#: A verdict saying the knowledge base does not cover what was described, so
#: the screenshot step falls through to the handover instead of answering.
NOT_COVERED = dict(
    answer="", confidence=0.1, covered_by_kb=False, topic="unknown", keywords=[]
)


def test_the_full_script_runs_as_written(session, judge):
    """The exact path the operator specified, end to end."""
    judge.verdict = Verdict(**NOT_COVERED)

    first = handle(msg("my creeb vpn is not connecting"), session, judge)
    assert first.action == REPLY
    assert "country/region" in first.reply_text
    assert not judge.called, "the questions themselves cost no model call"

    # An answer carrying no target keyword still reaches the flow.
    second = handle(msg("Nigeria/ghana mtn free 50mb"), session, judge)
    assert second.action == REPLY
    assert "latest app and config version" in second.reply_text

    third = handle(msg("yes"), session, judge)
    assert third.action == REPLY
    assert "screenshot" in third.reply_text
    assert not judge.called, "still no model call up to here"

    fourth = handle(msg("here it is", has_photo=True), session, judge)
    assert fourth.action == REPLY
    assert "beyond what I can answer" in fourth.reply_text
    assert "@creebadminbot" in fourth.reply_text


def test_a_described_problem_the_kb_covers_is_answered_not_handed_over(session, judge):
    """The screenshot step tries the knowledge base first.

    The log reasons are documented, so a described symptom should get an answer
    rather than being pushed to an admin.
    """
    judge.verdict = Verdict(
        answer="Switch to another tweak option and retry connecting the vpn.",
        confidence=0.95,
        covered_by_kb=True,
        topic="log reason",
        keywords=["tweak"],
    )

    handle(msg("my creeb vpn is not connecting"), session, judge)
    handle(msg("Nigeria MTN"), session, judge)
    handle(msg("yes"), session, judge)

    answered = handle(msg("the log says tweak blocked", has_photo=True), session, judge)

    assert answered.action == REPLY
    assert "Switch to another tweak option" in answered.reply_text
    assert "@creebadminbot" not in answered.reply_text
    assert session.scalar(select(Conversation)).state == FINISHED
    assert session.scalar(select(Query)) is None, "answered, so nothing to escalate"


def test_a_rule_still_holds_a_description_mid_flow(session, judge):
    """Consulting the knowledge base must not bypass the rules layer."""
    judge.verdict = Verdict(
        answer="Sure, refunded.", confidence=0.99, covered_by_kb=True,
        topic="refund", keywords=["refund"],
    )

    handle(msg("my creeb vpn is not connecting"), session, judge)
    handle(msg("Nigeria MTN"), session, judge)
    handle(msg("yes"), session, judge)

    result = handle(msg("i want a refund for this", has_photo=True), session, judge)

    assert "Sure, refunded." not in (result.reply_text or "")
    assert "beyond what I can answer" in result.reply_text


def test_answering_no_sends_them_to_update_and_ends(session, judge):
    handle(msg("my vpn is not working"), session, judge)
    handle(msg("Nigeria MTN"), session, judge)
    done = handle(msg("no"), session, judge)

    assert "Play Store" in done.reply_text
    assert "update config" in done.reply_text

    convo = session.scalar(select(Conversation))
    assert convo.state == FINISHED
    assert session.scalar(select(Query)) is None, "a resolved flow files nothing"


def test_escalation_files_a_queue_row_carrying_the_transcript(session, judge):
    judge.verdict = Verdict(**NOT_COVERED)
    for text, photo in [
        ("my creeb is not working", False),
        ("Ghana MTN free 50mb", False),
        ("yes", False),
        ("screenshot attached", True),
    ]:
        handle(msg(text, has_photo=photo), session, judge)

    query = session.scalar(select(Query))
    assert query is not None
    assert query.state == UNANSWERED
    assert query.body == "my creeb is not working", "the original complaint leads"

    # The admin opens the row already knowing what was collected.
    assert "Ghana MTN free 50mb" in query.transcript
    assert "screenshot attached" in query.transcript
    assert "country/region" in query.transcript


def test_two_people_can_be_in_the_flow_at_once(session, judge):
    a = handle(msg("my vpn is not connecting", user_id=1), session, judge)
    b = handle(msg("my creeb stop working", user_id=2), session, judge)
    assert "country/region" in a.reply_text and "country/region" in b.reply_text

    # Person 1 advances; person 2 must be untouched.
    handle(msg("Nigeria MTN", user_id=1), session, judge)

    convos = {c.user_id: c.step_id for c in session.scalars(select(Conversation))}
    assert convos[1] == "updated"
    assert convos[2] == "region"


def test_an_abandoned_flow_releases_the_person(session, judge, monkeypatch):
    handle(msg("my vpn is not working"), session, judge)
    convo = session.scalar(select(Conversation))

    # Walk away for longer than the timeout.
    convo.updated_at = convo.updated_at - timedelta(minutes=FLOWS.timeout_minutes + 5)
    session.flush()

    # An ordinary question now goes to the normal pipeline, not the stale flow.
    result = handle(msg("when is the payout processed?"), session, judge)
    assert result.action == REPLY
    assert judge.called, "the model handles it once the flow has lapsed"
    assert session.scalar(select(Conversation)).state == ABANDONED


def test_a_flow_beats_the_keyword_gate_and_the_model(session, judge):
    """"my vpn is not working" contains no target keyword in the fixture.

    It must still be caught, because the flow layer runs before the gate.
    """
    from app.kb import match_keywords

    ctx_terms = ["payout", "withdrawal", "verification", "refund", "log in"]
    assert match_keywords("my vpn is not working", ctx_terms) == []

    result = handle(msg("my vpn is not working"), session, judge)
    assert result.action == REPLY
    assert not judge.called


def test_commands_never_start_a_flow(session, judge):
    assert handle(msg("/start"), session, judge).action == IGNORE
    assert session.scalar(select(Conversation)) is None


def test_ordinary_questions_still_reach_the_model(session, judge):
    """The flow layer must not swallow everything else."""
    result = handle(msg("when is the payout processed?"), session, judge)
    assert result.action == REPLY
    assert judge.called
    assert session.scalar(select(Conversation)) is None


# --- Transcript ------------------------------------------------------------


def test_transcript_reads_as_a_conversation():
    text = render_transcript(
        [
            {"role": "user", "text": "my vpn is not working"},
            {"role": "bot", "text": "What's your country/region?"},
            {"role": "user", "text": "", "photo": True},
        ]
    )
    assert text.splitlines() == [
        "User: my vpn is not working",
        "Bot: What's your country/region?",
        "User: [screenshot attached]",
    ]
