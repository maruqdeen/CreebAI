"""Closing pleasantries.

From a real group: someone said "Ok thank you" at the screenshot step and the
bot read it as their answer, advanced the flow, and escalated a satisfied user
to an admin. Acknowledgements now run ahead of everything else.
"""

import pytest
from sqlalchemy import select

from app.flows import ACK, THANKS, classify_acknowledgement, load_flows, yes_or_no
from app.models import FINISHED, Conversation, Query
from app.pipeline import IGNORE, REPLY, Inbound, handle


def msg(text: str, user_id: int = 555, **kw) -> Inbound:
    return Inbound(
        text=text,
        channel="telegram",
        chat_id=-100,
        message_id=kw.pop("message_id", 1),
        user_id=user_id,
        user_handle="@mayor",
        **kw,
    )


# --- Classification --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["thanks", "Thank you", "thank you so much", "Ok thank you", "thanks bro",
     "tanx", "thx", "ty", "appreciate it", "THANKS!!", "thanks 🙏"],
)
def test_gratitude_is_recognised(text):
    assert classify_acknowledgement(text) == THANKS


@pytest.mark.parametrize(
    "text",
    ["ok", "Ok", "OK", "okay", "k", "kk", "alright", "noted", "cool", "great",
     "ok 👍", "👍", "perfect"],
)
def test_plain_acknowledgement_is_recognised(text):
    assert classify_acknowledgement(text) == ACK


@pytest.mark.parametrize(
    "text",
    [
        "thanks, how do i update config?",   # gratitude plus a real question
        "ok but it still not working",       # acknowledgement plus a complaint
        "ok i will try that and get back",
        "thanks for nothing it still fails",
        "my vpn is not connecting",
        "Nigeria MTN free 50mb",
    ],
)
def test_messages_with_substance_are_not_acknowledgements(text):
    """These must reach the flow or the model, not be swallowed as pleasantry."""
    assert classify_acknowledgement(text) is None


def test_ok_is_no_longer_read_as_yes():
    """Regression: "ok" answering a yes/no question advanced a step nobody
    had actually answered."""
    assert yes_or_no("ok") is None
    assert yes_or_no("okay") is None
    assert yes_or_no("yes") == "yes"


# --- Behaviour in a conversation -------------------------------------------


def test_thanks_gets_a_friendly_reply(session, judge):
    result = handle(msg("thank you"), session, judge)

    assert result.action == REPLY
    assert "You're welcome" in result.reply_text
    assert "@creebadminbot" in result.reply_text
    assert not judge.called, "a pleasantry must not cost a model call"


def test_bare_ok_gets_silence(session, judge):
    result = handle(msg("ok"), session, judge)

    assert result.action == IGNORE
    assert result.reply_text is None
    assert not judge.called
    assert session.scalar(select(Query)) is None, "nothing to log"


def test_thanks_mid_flow_closes_it_instead_of_escalating(session, judge):
    """The exact failure seen in the group."""
    handle(msg("my creeb vpn is not connecting"), session, judge)
    handle(msg("Nigeria/ghana mtn free 50mb"), session, judge)
    handle(msg("yes"), session, judge)  # now waiting for a screenshot

    result = handle(msg("Ok thank you"), session, judge)

    assert "You're welcome" in result.reply_text
    assert "logged this for an admin" not in (result.reply_text or "")
    assert session.scalar(select(Conversation)).state == FINISHED
    assert session.scalar(select(Query)) is None, "a satisfied user is not escalated"


def test_bare_ok_mid_flow_closes_it_quietly(session, judge):
    handle(msg("my vpn is not working"), session, judge)
    result = handle(msg("ok"), session, judge)

    assert result.action == IGNORE
    assert session.scalar(select(Conversation)).state == FINISHED


def test_a_real_answer_still_advances_the_flow(session, judge):
    """The acknowledgement layer must not swallow genuine answers."""
    handle(msg("my vpn is not working"), session, judge)
    result = handle(msg("Nigeria MTN free 50mb"), session, judge)

    assert result.action == REPLY
    assert "latest app and config version" in result.reply_text
    assert session.scalar(select(Conversation)).state != FINISHED


def test_thanking_then_asking_again_works(session, judge):
    """Closing a conversation must not make the person unable to start another."""
    handle(msg("thanks"), session, judge)
    result = handle(msg("my creeb vpn is not connecting"), session, judge)

    assert "country/region" in result.reply_text


# --- Operator-editable copy ------------------------------------------------


def test_the_thanks_reply_is_editable_in_flows_yaml():
    assert "You're welcome" in load_flows().reply("thanks")
