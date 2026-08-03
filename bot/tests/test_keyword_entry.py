"""Keywords are gate terms, not questions.

An operator pasted a list of full question headings from the knowledge base
into the keyword field. The save failed with a 500, because `keywords.term` is
64 characters — but even had it saved, none would ever have matched: the gate
looks for terms *inside* what a user types, and nobody types a 79-character
heading.
"""

import pytest
from conftest import make_client
from sqlalchemy import select

from app.kb import ANSWER_CONTRACT, match_keywords
from app.models import Keyword

A_WHOLE_QUESTION = (
    "Can't Add Time/ Ads is not showing / add timer is not working / I can't add the"
)


@pytest.fixture
def client(session, monkeypatch):
    return make_client(session, monkeypatch)


# --- Why a question cannot be a keyword ------------------------------------


def test_a_question_heading_never_matches_what_a_user_types():
    """The premise: the gate needs the phrase to appear in the message."""
    assert match_keywords("i cant add time", [A_WHOLE_QUESTION]) == []
    assert match_keywords("ads is not showing", [A_WHOLE_QUESTION]) == []


def test_the_subject_words_do_match():
    """What belongs in the field instead."""
    gate = ["add time", "ads", "timer"]
    assert match_keywords("i cant add time", gate) == ["add time"]
    assert match_keywords("ads is not showing", gate) == ["ads"]
    assert match_keywords("the add timer is not working", gate) == ["timer"]


# --- The save --------------------------------------------------------------


def test_an_over_long_keyword_is_refused_with_an_explanation(client):
    response = client.put(
        "/api/settings/telegram", json={"keywords": ["add time", A_WHOLE_QUESTION]}
    )
    assert response.status_code == 422

    detail = response.json()["detail"]
    assert "64 characters" in detail
    assert "whole questions" in detail.lower()
    assert "knowledge base" in detail.lower()


def test_a_refused_save_leaves_the_existing_keywords_alone(client, session):
    """Validation happens before the delete: a rejected save must not wipe."""
    before = set(session.scalars(select(Keyword.term)))
    assert before, "fixture should start with keywords"

    client.put("/api/settings/telegram", json={"keywords": [A_WHOLE_QUESTION]})

    assert set(session.scalars(select(Keyword.term))) == before


def test_a_normal_list_still_saves(client):
    response = client.put(
        "/api/settings/telegram",
        json={"keywords": ["add time", "ads", "dns", "mtn", "glo", "airplane mode"]},
    )
    assert response.status_code == 200
    assert "add time" in response.json()["keywords"]


def test_a_keyword_right_at_the_limit_is_accepted(client):
    assert (
        client.put("/api/settings/telegram", json={"keywords": ["a" * 64]}).status_code
        == 200
    )


# --- Slash-separated headings ----------------------------------------------


def test_the_contract_reads_a_slash_as_or():
    """The knowledge base lists variants; matching one branch is coverage."""
    lowered = ANSWER_CONTRACT.lower()
    assert "alternative phrasings" in lowered
    assert "read those as or" in lowered
    assert "matched only one branch" in lowered


def test_the_contract_shows_a_real_heading_as_the_example():
    assert "Ads is not showing" in ANSWER_CONTRACT
