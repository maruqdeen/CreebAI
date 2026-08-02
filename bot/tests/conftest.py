"""Test fixtures: an in-memory database and a stubbed judge.

No network, no Telegram, no Groq key required.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.groq_client import Verdict  # noqa: E402
from app.models import Base, ChannelSettings, Keyword, Rule  # noqa: E402


class StubJudge:
    """A `Judge` that returns whatever the test tells it to, and records calls."""

    def __init__(self, verdict: Verdict | None = None):
        self.verdict = verdict or Verdict(
            answer="Payouts run every Tuesday and Friday.",
            confidence=0.91,
            covered_by_kb=True,
            topic="payout schedule",
            keywords=["payout", "schedule"],
            latency_ms=120,
        )
        self.calls: list[tuple[str, str, list[str]]] = []

    def judge(self, message: str, kb_text: str, rules: list[str]) -> Verdict:
        self.calls.append((message, kb_text, rules))
        return self.verdict

    @property
    def called(self) -> bool:
        return bool(self.calls)


@pytest.fixture
def judge() -> StubJudge:
    return StubJudge()


def make_client(session, monkeypatch, *, signed_in: bool = True):
    """A TestClient wired to the test session.

    `signed_in=True` stands in for a logged-in operator so the existing tests
    exercise the endpoints rather than the door. `signed_in=False` leaves the
    real guard in place — that is how test_auth.py checks the door works.
    """
    from fastapi.testclient import TestClient

    from app.api import create_app, require_admin
    from app.db import get_session

    monkeypatch.setattr("app.api.init_db", lambda: None)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    if signed_in:
        app.dependency_overrides[require_admin] = lambda: "admin"
    return TestClient(app)


@pytest.fixture
def session() -> Session:
    # StaticPool keeps every connection pointed at the same in-memory database,
    # and check_same_thread=False lets TestClient reach it from the worker
    # thread it runs sync endpoints on.
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = maker()

    db.add(
        ChannelSettings(
            channel="telegram",
            kb_text="Payouts run every Tuesday and Friday and land within 24 hours.",
            reply_threshold=0.62,
        )
    )
    for term in ("payout", "withdrawal", "verification", "refund", "log in"):
        db.add(Keyword(channel="telegram", term=term))
    db.add(
        Rule(
            channel="telegram",
            ref="R-01",
            text="Hold every complaint for a human reply.",
            triggers="unacceptable, scam, terrible",
            position=0,
        )
    )
    db.add(
        Rule(
            channel="telegram",
            ref="R-04",
            text="Hold every refund request for a human reply.",
            triggers="refund, chargeback",
            position=1,
        )
    )
    db.add(
        Rule(
            channel="telegram",
            ref="R-02",
            text="Never state a fee that is not in the knowledge base.",
            triggers="",  # advisory only: shapes the prompt, enforces nothing
            position=2,
        )
    )
    db.commit()

    try:
        yield db
    finally:
        db.close()
