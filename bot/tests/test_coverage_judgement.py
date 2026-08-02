"""What "covered by the knowledge base" means, and rate-limit retries.

From the group: someone wrote "Creeb crash on my phone" and the bot answered
with a general "if it stops working, do these 3 things" list. The model had
rated that 0.90 covered — it reached for the nearest passage rather than the
right one, because the contract never said those are different things.
"""

import pytest

from app.groq_client import MAX_RETRY_WAIT, _retry_after
from app.kb import ANSWER_CONTRACT, build_system_prompt


# --- The coverage contract -------------------------------------------------


def test_the_contract_separates_same_subject_from_answered():
    lowered = ANSWER_CONTRACT.lower()
    assert "being in the same subject is not coverage" in lowered
    assert "closest passage rather than the right one" in lowered


def test_the_contract_names_the_failure_that_happened():
    """A concrete example beats an abstract rule, so keep it concrete."""
    lowered = ANSWER_CONTRACT.lower()
    assert "crashes when i open it" in lowered
    assert "error 403" in lowered


def test_the_confidence_scale_is_anchored_not_vague():
    lowered = ANSWER_CONTRACT.lower()
    for anchor in ("1.0", "0.8", "0.5", "0.2", "0.0"):
        assert anchor in lowered
    # The floor that stops an adjacent passage being treated as an answer.
    assert "at 0.5 or below" in lowered


def test_escalating_is_framed_as_cheap_and_answering_wrongly_as_costly():
    lowered = ANSWER_CONTRACT.lower()
    assert "costs almost nothing" in lowered
    assert "teaches them not to trust you" in lowered


def test_the_coverage_rules_reach_the_model():
    prompt = build_system_prompt("Payouts run Tuesday.", [])
    assert "COVERAGE" in prompt
    assert "Payouts run Tuesday." in prompt


# --- Rate-limit retry ------------------------------------------------------


def test_the_wait_is_read_from_groqs_own_message():
    error = (
        "RateLimitError: Error code: 429 - Rate limit reached for model "
        "`openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Used "
        "2153, Requested 7213. Please try again in 10.245s."
    )
    # A small margin on top, because the window is on Groq's clock.
    assert _retry_after(error) == pytest.approx(10.745)


def test_an_unparseable_error_still_waits_a_sensible_amount():
    assert _retry_after("RateLimitError: something unexpected") == 5.0
    assert _retry_after("") == 5.0


def test_a_long_stated_wait_is_capped():
    """A worker thread must not be held indefinitely by a remote number."""
    assert _retry_after("try again in 600s") == MAX_RETRY_WAIT


def test_only_rate_limits_are_retried(monkeypatch):
    """An auth failure retried is just a second failure and a slower reply."""
    from app.groq_client import GroqJudge, Verdict

    judge = GroqJudge(api_key="k")
    calls = []

    def fail(message, kb_text, rules):
        calls.append(message)
        return Verdict.failed("AuthenticationError: invalid api key")

    monkeypatch.setattr(judge, "_ask", fail)
    verdict = judge.judge("anything", "kb", [])

    assert len(calls) == 1
    assert not verdict.ok


def test_a_rate_limited_call_is_retried_once(monkeypatch):
    from app.groq_client import GroqJudge, Verdict

    judge = GroqJudge(api_key="k")
    calls = []

    def flaky(message, kb_text, rules):
        calls.append(message)
        if len(calls) == 1:
            return Verdict.failed("RateLimitError: try again in 0.01s")
        return Verdict(
            answer="Tuesday and Friday.", confidence=0.9, covered_by_kb=True,
            topic="payouts", keywords=[],
        )

    monkeypatch.setattr(judge, "_ask", flaky)
    verdict = judge.judge("when is payout", "kb", [])

    assert len(calls) == 2, "a rate limit is a wait, not a failure"
    assert verdict.ok and verdict.answer == "Tuesday and Friday."


def test_a_still_rate_limited_retry_fails_closed(monkeypatch):
    from app.groq_client import GroqJudge, Verdict

    judge = GroqJudge(api_key="k")
    monkeypatch.setattr(
        judge, "_ask", lambda *a: Verdict.failed("RateLimitError: try again in 0.01s")
    )
    verdict = judge.judge("when is payout", "kb", [])

    assert not verdict.ok
    assert verdict.covered_by_kb is False, "never answers on a failed call"
