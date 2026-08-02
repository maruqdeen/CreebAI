"""API contract tests.

Every assertion here is about a shape the built panel already renders, so a
break in this file means the panel would break too.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.db import get_session
from app.groq_client import Verdict
from app.models import ANSWERED, KBEntry, Query
from app.pipeline import Inbound, handle


@pytest.fixture
def client(session, monkeypatch):
    from conftest import make_client

    return make_client(session, monkeypatch)


def feed(session, judge, text: str, **verdict_kw):
    if verdict_kw:
        judge.verdict = Verdict(**verdict_kw)
    return handle(
        Inbound(
            text=text, channel="telegram", chat_id=-100, message_id=7,
            user_id=5, user_handle="@adaeze_k",
        ),
        session,
        judge,
    )


# --- Health ----------------------------------------------------------------


def test_health_reports_what_is_still_unconfigured(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "model" in body
    assert isinstance(body["missing_settings"], list)


# --- Queue -----------------------------------------------------------------


def test_queue_returns_the_fields_the_panel_renders(client, session, judge):
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    rows = client.get("/api/queries", params={"state": "unanswered"}).json()
    assert len(rows) == 1

    row = rows[0]
    for field in ("ref", "state", "user_handle", "body", "received_at", "reason", "new_keywords"):
        assert field in row, f"panel reads {field}"
    assert row["ref"] == "TG-0001"
    assert "kyc" in row["new_keywords"]


def test_queue_filters_by_state(client, session, judge):
    feed(session, judge, "when is the payout processed?")  # answered
    feed(session, judge, "how long does the KYC review take?")  # queued
    session.commit()

    assert len(client.get("/api/queries", params={"state": "answered"}).json()) == 1
    assert len(client.get("/api/queries", params={"state": "unanswered"}).json()) == 1
    assert len(client.get("/api/queries").json()) == 2


def test_missing_query_is_a_404(client):
    assert client.get("/api/queries/TG-9999").status_code == 404


# --- Reply -----------------------------------------------------------------


def test_reply_marks_answered_and_files_to_the_knowledge_base(client, session, judge):
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    res = client.post(
        "/api/queries/TG-0001/reply",
        json={"text": "KYC review takes up to 48 hours.", "file_to_kb": True,
              "kb_title": "KYC review time"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == ANSWERED
    assert body["filed_kb_entry_id"] is not None

    query = session.scalar(select(Query).where(Query.ref == "TG-0001"))
    assert query.answered_by == "admin"
    assert query.answer_text == "KYC review takes up to 48 hours."

    entry = session.scalar(select(KBEntry))
    assert entry.title == "KYC review time"
    assert entry.source == "filed"


def test_filing_an_answer_closes_the_loop(client, session, judge):
    """The promise the composer makes: file it once, the bot handles it next time.

    Both halves have to happen. Filing writes knowledge the model can read, and
    promotes the discovered terms so the keyword gate stops sending the same
    question to the queue.
    """
    first = feed(session, judge, "how long does the KYC review take?")
    assert first.action == "queue"
    assert not judge.called, "unknown topic: captured, no model call"
    session.commit()

    body = client.post(
        "/api/queries/TG-0001/reply",
        json={"text": "KYC review takes up to 48 hours.", "file_to_kb": True,
              "kb_title": "KYC review time"},
    ).json()
    assert "kyc" in body["promoted_keywords"]
    session.expire_all()

    # Same question again: it now clears the gate and reaches the model...
    again = feed(session, judge, "how long does the KYC review take?")
    assert again.action == "reply", "the gate must now let this through"
    assert judge.called

    # ...and the filed answer is actually in the prompt the model saw.
    _, kb_sent, _ = judge.calls[-1]
    assert "KYC review takes up to 48 hours." in kb_sent
    assert "KYC review time" in kb_sent


def test_filed_answers_reach_the_model_even_without_promotion(client, session, judge):
    """A filed answer must be in the prompt regardless of the gate."""
    feed(session, judge, "how long does the KYC review take?")
    session.commit()
    client.post(
        "/api/queries/TG-0001/reply",
        json={"text": "KYC review takes up to 48 hours.", "file_to_kb": True},
    )
    session.expire_all()

    feed(session, judge, "when is the payout processed?")  # a different topic
    _, kb_sent, _ = judge.calls[-1]
    assert "KYC review takes up to 48 hours." in kb_sent


def test_promotion_is_capped(client, session, judge):
    """Filing one answer must not blow the gate wide open."""
    from app.api import MAX_PROMOTED_PER_REPLY

    feed(session, judge, "why does zebra igloo cactus velvet quartz jigsaw fondue matter?")
    session.commit()

    body = client.post(
        "/api/queries/TG-0001/reply", json={"text": "Because.", "file_to_kb": True}
    ).json()
    assert 0 < len(body["promoted_keywords"]) <= MAX_PROMOTED_PER_REPLY


def test_declining_to_file_promotes_nothing(client, session, judge):
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    body = client.post(
        "/api/queries/TG-0001/reply",
        json={"text": "Up to 48 hours.", "file_to_kb": False},
    ).json()

    assert body["promoted_keywords"] == []
    assert body["filed_kb_entry_id"] is None
    assert "kyc" not in client.get("/api/settings/telegram").json()["keywords"]


def test_reply_reports_delivery_failure_honestly(client, session, judge):
    """No bot token here, so the send cannot succeed — say so, don't claim it did."""
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    body = client.post(
        "/api/queries/TG-0001/reply", json={"text": "Up to 48 hours."}
    ).json()

    assert body["delivered"] is False
    assert body["delivery_error"], "the panel must be able to show why"
    # The knowledge is still captured even though the message did not land.
    assert body["filed_kb_entry_id"] is not None


def test_replying_twice_is_rejected(client, session, judge):
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    client.post("/api/queries/TG-0001/reply", json={"text": "first"})
    assert client.post("/api/queries/TG-0001/reply", json={"text": "second"}).status_code == 409


def test_reply_requires_text(client, session, judge):
    feed(session, judge, "how long does the KYC review take?")
    session.commit()
    assert client.post("/api/queries/TG-0001/reply", json={"text": ""}).status_code == 422


# --- Keywords --------------------------------------------------------------


def test_new_keywords_endpoint_ranks_by_how_often_they_were_asked(client, session, judge):
    feed(session, judge, "how long does the KYC review take?")
    feed(session, judge, "is KYC required for everyone?")
    session.commit()

    rows = client.get("/api/keywords/new").json()
    terms = [r["term"] for r in rows]
    assert terms[0] == "kyc", "the most-asked uncovered term leads"
    assert rows[0]["count"] == 2


# --- Dashboard -------------------------------------------------------------


def test_dashboard_totals_and_channel_split(client, session, judge):
    feed(session, judge, "when is the payout processed?")
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    body = client.get("/api/dashboard", params={"period": "week"}).json()
    assert body["total_answered"] == 1

    telegram = next(c for c in body["channels"] if c["channel"] == "telegram")
    assert telegram["answered"] == 1
    assert telegram["unanswered"] == 1
    assert telegram["median_reply_ms"] == 120


def test_dashboard_most_asked_labels_capture_rows_by_their_question(client, session, judge):
    """Silent-capture rows have no topic; they must still appear, by question."""
    feed(session, judge, "how long does the KYC review take?")
    session.commit()

    items = client.get("/api/dashboard").json()["most_asked"]["telegram"]
    assert any("KYC" in i["question"] for i in items)
    assert items[0]["in_kb"] is False


def test_dashboard_rejects_an_unknown_period(client):
    assert client.get("/api/dashboard", params={"period": "decade"}).status_code == 422


# --- Settings --------------------------------------------------------------


def test_settings_round_trip(client, session):
    before = client.get("/api/settings/telegram").json()
    assert before["reply_threshold"] == 0.62
    assert "payout" in before["keywords"]

    res = client.put(
        "/api/settings/telegram",
        json={
            "reply_threshold": 0.8,
            "kb_text": "Payouts run on Tuesday only.",
            "keywords": ["payout", "kyc", "PAYOUT"],
            "rules": [{"ref": "R-09", "text": "Escalate anything about tax.",
                       "triggers": "tax, hmrc", "active": True}],
        },
    )
    assert res.status_code == 200

    after = res.json()
    assert after["reply_threshold"] == 0.8
    assert after["kb_text"] == "Payouts run on Tuesday only."
    assert sorted(after["keywords"]) == ["kyc", "payout"], "duplicates and case collapse"
    assert [r["ref"] for r in after["rules"]] == ["R-09"]


def test_partial_settings_update_leaves_the_rest_alone(client, session):
    client.put("/api/settings/telegram", json={"reply_threshold": 0.75})
    after = client.get("/api/settings/telegram").json()

    assert after["reply_threshold"] == 0.75
    assert "payout" in after["keywords"], "keywords untouched when not supplied"
    assert after["kb_text"], "knowledge base untouched when not supplied"


def test_threshold_is_bounded(client):
    assert client.put("/api/settings/telegram", json={"reply_threshold": 4}).status_code == 422


def test_unknown_channel_is_a_404(client):
    assert client.get("/api/settings/carrier-pigeon").status_code == 404


def test_a_new_threshold_actually_changes_the_decision(client, session, judge):
    """The settings page is only meaningful if the pipeline reads it."""
    judge.verdict = Verdict(
        answer="Tuesday and Friday.", confidence=0.70, covered_by_kb=True,
        topic="payouts", keywords=["payout"],
    )
    assert feed(session, judge, "when is the payout processed?").action == "reply"

    client.put("/api/settings/telegram", json={"reply_threshold": 0.9})
    session.expire_all()

    assert feed(session, judge, "when is the payout processed?").action == "queue"
