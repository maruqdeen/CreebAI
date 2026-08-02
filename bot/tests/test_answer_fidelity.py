"""The answer contract and long-answer delivery.

Answers reproduce the knowledge base rather than summarising it, because the
parts people act on — steps, numbers, menu labels — are wrong the moment they
are reworded. That makes answers longer, which makes splitting them for
delivery load-bearing rather than an edge case.
"""

import pytest

from app.kb import ANSWER_CONTRACT, build_system_prompt
from app.telegram_bot import TELEGRAM_MAX_CHARS, split_for_telegram


# --- The contract ----------------------------------------------------------


def test_contract_frames_the_job_as_quoting_not_writing():
    lowered = ANSWER_CONTRACT.lower()
    assert "you are quoting" in lowered
    assert "word for word" in lowered
    assert "copying is the default" in lowered
    assert "do not rewrite" in lowered


def test_contract_forbids_compressing_a_procedure():
    lowered = ANSWER_CONTRACT.lower()
    assert "never compress a procedure" in lowered
    # A worked example anchors the rule far better than the rule alone.
    assert "worked example" in lowered
    assert "step 1:" in lowered


def test_contract_prefers_the_fullest_passage_when_the_kb_repeats_itself():
    """A knowledge base often answers the same question twice.

    A one-line summary elsewhere in the document must not win over the
    procedure someone can actually follow.
    """
    lowered = ANSWER_CONTRACT.lower()
    assert "fullest" in lowered
    assert "never overrides a detailed procedure" in lowered


def test_contract_no_longer_caps_the_answer_length():
    """Regression: the old contract said "two or three short sentences".

    That instruction is what turned a five-step setup guide into a one-line
    summary, and a summarised step is a step someone cannot follow.
    """
    lowered = ANSWER_CONTRACT.lower()
    assert "two or three short sentences" not in lowered
    assert "length is not a problem" in lowered


def test_contract_still_forbids_inventing_anything():
    """Fidelity must not come at the cost of the safety instructions."""
    lowered = ANSWER_CONTRACT.lower()
    assert "covered_by_kb to false" in lowered
    assert "do not improvise" in lowered
    assert "never state a fee" in lowered
    assert "never ask for a password" in lowered


def test_contract_reaches_the_model():
    prompt = build_system_prompt("Payouts run Tuesday.", [])
    assert "you are quoting" in prompt.lower()
    assert "Payouts run Tuesday." in prompt


# --- Splitting long answers ------------------------------------------------


def test_short_answers_are_one_message():
    assert split_for_telegram("Payouts run Tuesday and Friday.") == [
        "Payouts run Tuesday and Friday."
    ]


def test_empty_answer_produces_nothing_to_send():
    assert split_for_telegram("") == []
    assert split_for_telegram("   ") == []


def test_nothing_is_lost_when_splitting():
    """The whole point: a long procedure must arrive complete."""
    steps = "\n\n".join(f"Step {n}: do the thing carefully." for n in range(1, 400))
    parts = split_for_telegram(steps)

    assert len(parts) > 1
    for n in (1, 200, 399):
        assert f"Step {n}: do the thing carefully." in " ".join(parts)


def test_every_part_is_within_the_telegram_limit():
    steps = "\n\n".join(f"Step {n}: do the thing carefully." for n in range(1, 400))
    assert all(len(part) <= TELEGRAM_MAX_CHARS for part in split_for_telegram(steps))


def test_splitting_prefers_a_paragraph_boundary():
    """A numbered step must never be cut in half."""
    steps = "\n\n".join(f"Step {n}: " + "x" * 200 for n in range(1, 60))
    for part in split_for_telegram(steps):
        assert part.startswith("Step "), "each part begins at a step, not mid-step"


def test_an_unbroken_run_still_terminates():
    """No boundary to find must not mean an infinite loop."""
    parts = split_for_telegram("x" * (TELEGRAM_MAX_CHARS * 3 + 17))
    assert len(parts) == 4
    assert sum(len(p) for p in parts) == TELEGRAM_MAX_CHARS * 3 + 17


@pytest.mark.parametrize("size", [4095, 4096, 4097, 8192])
def test_boundaries_around_the_limit(size):
    parts = split_for_telegram("a" * size)
    assert all(len(p) <= TELEGRAM_MAX_CHARS for p in parts)
    assert "".join(parts) == "a" * size
