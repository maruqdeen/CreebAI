"""The text layer: keyword gate, question heuristic, keyword extraction."""

import pytest

from app.groq_client import Verdict, parse_verdict
from app.kb import (
    MAX_TERM_LEN,
    build_system_prompt,
    extract_candidate_keywords,
    looks_like_question,
    match_keywords,
    sane_term,
)

TERMS = ["payout", "log in", "verification", "account"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("when is the payout processed", ["payout"]),
        ("PAYOUT please", ["payout"]),
        ("i cannot log in", ["log in"]),
        ("i cannot log    in", ["log in"]),  # tolerant of spacing
        ("payout and account issues", ["payout", "account"]),
        ("", []),
    ],
)
def test_keyword_matching(text, expected):
    assert match_keywords(text, TERMS) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "i cant connect",
        "i can't connect",  # straight apostrophe
        "i can’t connect",  # curly — what a phone keyboard produces
        "I CAN'T CONNECT",
    ],
)
def test_apostrophes_are_folded_on_both_sides(typed):
    """A trigger written "cant connect" must catch every way it gets typed."""
    assert match_keywords(typed, ["cant connect"]) == ["cant connect"]


@pytest.mark.parametrize(
    "text",
    ["my accountant called", "i want to diversify", "logging is broken", "payouts"],
)
def test_keyword_matching_respects_word_boundaries(text):
    """Substring matching would make the gate fire constantly on false hits."""
    assert match_keywords(text, ["account", "verify", "log in", "payout"]) == []


@pytest.mark.parametrize(
    "text",
    [
        "how long does this take?",
        "how long does this take",  # opener, no question mark
        "does anyone know the fee",
        "so how do i do this",  # opener behind a short filler
        "can i use two phones",
        "this is unacceptable",  # complaint marker
        "the app is a scam",
    ],
)
def test_question_and_complaint_shapes_are_detected(text):
    assert looks_like_question(text) is True


@pytest.mark.parametrize(
    "text",
    ["thanks everyone", "gm", "lol same here", "ok cool", ""],
)
def test_ordinary_chatter_is_not_a_question(text):
    assert looks_like_question(text) is False


def test_extraction_drops_stopwords_and_short_tokens():
    terms = extract_candidate_keywords("How long does the KYC review take?")
    assert "kyc" in terms and "review" in terms
    assert "how" not in terms and "the" not in terms
    assert all(len(t) >= 3 for t in terms)


def test_extraction_preserves_first_seen_order_and_dedupes():
    assert extract_candidate_keywords("kyc kyc review kyc") == ["kyc", "review"]


def test_extraction_respects_the_limit():
    text = " ".join(f"term{i}" for i in range(30))
    assert len(extract_candidate_keywords(text, limit=5)) == 5


def test_absurdly_long_tokens_are_never_stored_as_keywords():
    """Regression: `QueryKeyword.term` is String(64).

    A pasted URL or a mashed-keyboard "word" used to be extracted verbatim,
    which floods the escalation DM and would be rejected outright by Postgres.
    """
    assert extract_candidate_keywords("payout " + "x" * 2000 + "?") == ["payout"]
    assert sane_term("x" * 2000) is None
    assert all(len(t) <= MAX_TERM_LEN for t in extract_candidate_keywords("a" * 100))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Payout  ", "payout"),
        ("'quoted'", "quoted"),
        ("no", None),  # under the minimum length
        ("2026", None),  # bare number
        ("the", None),  # stopword
        ("", None),
        ("x" * 33, None),  # over the maximum
    ],
)
def test_sane_term_normalises_and_rejects(raw, expected):
    assert sane_term(raw) == expected


def test_model_supplied_keywords_are_sanitised_at_the_boundary():
    """Nothing downstream should have to reason about the model's list."""
    v = parse_verdict(
        '{"answer":"x","confidence":0.9,"covered_by_kb":true,"topic":"t",'
        '"keywords":["Payout","the","' + "z" * 200 + '","payout","42"]}'
    )
    assert v.keywords == ["payout"]


def test_system_prompt_carries_the_kb_and_the_rules():
    prompt = build_system_prompt("Payouts run on Tuesday.", ["R-01: Hold complaints."])
    assert "Payouts run on Tuesday." in prompt
    assert "R-01: Hold complaints." in prompt
    assert "covered_by_kb" in prompt, "the contract must explain the verdict fields"


def test_system_prompt_survives_an_empty_kb():
    assert "(empty)" in build_system_prompt("", [])


# --- Verdict parsing -------------------------------------------------------


def test_parse_verdict_reads_a_well_formed_response():
    v = parse_verdict(
        '{"answer":"Tuesday and Friday.","confidence":0.9,"covered_by_kb":true,'
        '"topic":"payout schedule","keywords":["Payout","SCHEDULE"]}'
    )
    assert v.ok and v.answer == "Tuesday and Friday."
    assert v.confidence == 0.9 and v.covered_by_kb is True
    assert v.keywords == ["payout", "schedule"], "keywords are normalised to lowercase"


@pytest.mark.parametrize("raw", ["not json at all", "", "[1,2,3]", "null"])
def test_unparseable_responses_fail_closed(raw):
    v = parse_verdict(raw)
    assert not v.ok
    assert v.covered_by_kb is False and v.confidence == 0.0


def test_confidence_is_clamped_to_the_unit_interval():
    assert parse_verdict('{"confidence":9.5,"covered_by_kb":true}').confidence == 1.0
    assert parse_verdict('{"confidence":-3,"covered_by_kb":true}').confidence == 0.0
    assert parse_verdict('{"confidence":"high","covered_by_kb":true}').confidence == 0.0


def test_failed_verdict_never_looks_answerable():
    v = Verdict.failed("timeout")
    assert not v.ok and not v.covered_by_kb and v.confidence == 0.0 and v.answer == ""
