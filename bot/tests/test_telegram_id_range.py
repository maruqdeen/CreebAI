"""Telegram ids do not fit in 32 bits.

From production: a supergroup chat id of -1004369410993 hit an INTEGER column
and Postgres raised "integer out of range" on every group message. SQLite
ignores declared column types, so local development and the whole test suite
passed while the deployed bot could not store a single conversation.
"""

import pytest
from sqlalchemy import BigInteger, inspect

from app.models import ChannelSettings, Conversation, Query

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

#: A real supergroup id, and a user id in the range Telegram has moved into.
REAL_SUPERGROUP_ID = -1004369410993
LARGE_USER_ID = 7_345_982_119


ID_COLUMNS = [
    (ChannelSettings, "group_chat_id"),
    (ChannelSettings, "admin_chat_id"),
    (Query, "tg_chat_id"),
    (Query, "tg_message_id"),
    (Query, "tg_user_id"),
    (Conversation, "chat_id"),
    (Conversation, "user_id"),
]


@pytest.mark.parametrize(
    "model, column", ID_COLUMNS, ids=[f"{m.__name__}.{c}" for m, c in ID_COLUMNS]
)
def test_every_telegram_id_column_is_64_bit(model, column):
    """Declared type, not behaviour — SQLite would let a narrow column pass."""
    type_ = inspect(model).columns[column].type
    assert isinstance(type_, BigInteger), (
        f"{model.__name__}.{column} must be BigInteger: Telegram ids exceed int32"
    )


def test_the_id_that_broke_production_is_out_of_int32_range():
    """Guards the premise, so this test file cannot quietly become vacuous."""
    assert REAL_SUPERGROUP_ID < INT32_MIN
    assert LARGE_USER_ID > INT32_MAX


def test_a_supergroup_conversation_round_trips(session):
    session.add(
        Conversation(
            channel="telegram",
            chat_id=REAL_SUPERGROUP_ID,
            user_id=LARGE_USER_ID,
            flow_id="connection-troubleshooting",
            step_id="region",
            state="active",
        )
    )
    session.flush()

    found = session.query(Conversation).one()
    assert found.chat_id == REAL_SUPERGROUP_ID
    assert found.user_id == LARGE_USER_ID


def test_a_query_from_a_supergroup_round_trips(session):
    session.add(
        Query(
            ref="TG-0001",
            channel="telegram",
            tg_chat_id=REAL_SUPERGROUP_ID,
            tg_user_id=LARGE_USER_ID,
            tg_message_id=4_294_967_296,  # deliberately past int32 too
            body="my creeb vpn is not connecting",
        )
    )
    session.flush()

    found = session.query(Query).one()
    assert found.tg_chat_id == REAL_SUPERGROUP_ID
    assert found.tg_user_id == LARGE_USER_ID


def test_the_pipeline_handles_a_real_supergroup_message(session, judge):
    """End to end: the exact shape that was failing in the group."""
    from app.pipeline import Inbound, handle

    decision = handle(
        Inbound(
            text="my creeb vpn is not connecting",
            channel="telegram",
            chat_id=REAL_SUPERGROUP_ID,
            message_id=1,
            user_id=LARGE_USER_ID,
            user_handle="@someone",
        ),
        session,
        judge,
    )
    assert decision.action == "reply"

    convo = session.query(Conversation).one()
    assert convo.chat_id == REAL_SUPERGROUP_ID
