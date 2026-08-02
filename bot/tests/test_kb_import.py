"""Deriving target keywords from knowledge base prose.

The bot goes mute on its own subject matter whenever the gate and the knowledge
base disagree, so these assertions are about that pairing staying honest.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import get_session
from app.kb import derive_keywords

VPN_KB = """
CreebVPN is a virtual private network for Android, iOS and Windows.

CONNECTING
- Open the app, pick a server, tap Connect. The first connect asks Android for
  VPN permission; that prompt is from Android, not from us.
- If the connect button spins and stops, switch server and try again.
- A connection that drops on mobile data but holds on wifi is usually battery
  optimisation killing the app in the background.

SERVERS
- Server locations are listed in the app. Pick the closest server for speed.
- A slow server is usually a busy server. Switch server before reporting speed.

SUBSCRIPTION
- Plans are monthly and yearly. A subscription covers five devices.
- Billing runs from the day you subscribed, not the first of the month.
- Cancelling stops the renewal; access continues to the end of the period paid for.
"""


# --- Derivation ------------------------------------------------------------


def test_derives_the_actual_subjects_of_the_text():
    terms = [t for t, _ in derive_keywords(VPN_KB)]

    for expected in ("server", "connect", "subscription", "app"):
        assert expected in terms, f"{expected!r} is plainly what this document is about"


def test_does_not_derive_stopwords_or_noise():
    terms = [t for t, _ in derive_keywords(VPN_KB)]
    for junk in ("the", "and", "that", "from", "you"):
        assert junk not in terms


def test_ranks_by_how_often_the_subject_recurs():
    ranked = derive_keywords(VPN_KB)
    counts = [n for _, n in ranked]
    assert counts == sorted(counts, reverse=True)
    assert ranked[0][1] >= 3, "the leading term should genuinely recur"


def test_single_mentions_are_not_promoted():
    """min_count guards against gating on a word used once in passing."""
    assert derive_keywords("A one off mention of zebras.", min_count=2) == []


def test_respects_the_limit():
    assert len(derive_keywords(VPN_KB, limit=3)) == 3


def test_exclude_leaves_out_terms_already_configured():
    terms = [t for t, _ in derive_keywords(VPN_KB, exclude=["server", "connect"])]
    assert "server" not in terms and "connect" not in terms
    assert terms, "excluding two terms should not empty the list"


def test_empty_input_is_not_an_error():
    assert derive_keywords("") == []
    assert derive_keywords("   \n  ") == []


def test_derived_terms_are_all_storable():
    """Everything returned has to fit QueryKeyword.term / Keyword.term."""
    from app.kb import MAX_TERM_LEN, MIN_TERM_LEN

    for term, _ in derive_keywords(VPN_KB + " " + "x" * 500):
        assert MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN


# --- The endpoint the panel will call --------------------------------------


@pytest.fixture
def client(session, monkeypatch):
    from conftest import make_client

    return make_client(session, monkeypatch)


def test_suggest_endpoint_derives_from_supplied_text_without_saving(client):
    before = client.get("/api/settings/telegram").json()["keywords"]

    rows = client.post(
        "/api/settings/telegram/keywords/suggest", json={"kb_text": VPN_KB}
    ).json()

    assert "server" in [r["term"] for r in rows]
    assert client.get("/api/settings/telegram").json()["keywords"] == before, (
        "suggesting must not save"
    )


def test_suggest_marks_which_terms_are_additions(client):
    rows = client.post(
        "/api/settings/telegram/keywords/suggest",
        json={"kb_text": VPN_KB + "\npayout payout payout"},
    ).json()
    by_term = {r["term"]: r["is_new"] for r in rows}

    assert by_term["server"] is True, "not currently configured"
    assert by_term["payout"] is False, "already a target keyword"


def test_suggest_falls_back_to_the_saved_knowledge_base(client):
    client.put("/api/settings/telegram", json={"kb_text": VPN_KB})
    rows = client.post("/api/settings/telegram/keywords/suggest", json={}).json()
    assert "server" in [r["term"] for r in rows]


def test_the_full_swap_makes_the_bot_answer_its_new_subject(client, session, judge):
    """The point of all of this, end to end.

    Import a VPN knowledge base with its derived keywords, and a VPN question
    that previously fell through the gate now reaches the model.
    """
    from app.pipeline import Inbound, handle

    def ask(text):
        return handle(
            Inbound(text=text, channel="telegram", chat_id=-1, message_id=1, user_id=1),
            session,
            judge,
        )

    assert ask("how do i connect creebvpn?").action == "queue"
    assert not judge.called, "no keyword matched, so the model was never consulted"

    suggested = [
        r["term"]
        for r in client.post(
            "/api/settings/telegram/keywords/suggest", json={"kb_text": VPN_KB}
        ).json()
    ]
    client.put(
        "/api/settings/telegram", json={"kb_text": VPN_KB, "keywords": suggested}
    )
    session.expire_all()

    assert ask("how do i connect creebvpn?").action == "reply"
    assert judge.called
    _, kb_sent, _ = judge.calls[-1]
    assert "CreebVPN is a virtual private network" in kb_sent
