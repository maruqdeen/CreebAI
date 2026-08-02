"""Config parsing and the DM rehearsal.

Both cover first-run experience: the state a fresh `.env` is actually in, and
the way the bot is tried out before it joins a group.
"""

import pytest

from app.config import Settings
from app.telegram_bot import Rehearsal, _format_rehearsal


# --- Config ----------------------------------------------------------------


@pytest.mark.parametrize("field", ["admin_telegram_user_id", "telegram_group_chat_id"])
def test_blank_optional_ints_mean_unset_not_invalid(field):
    """Regression: a copied .env has these blank, and it used to crash.

    `FOO=` in .env arrives as an empty string. Pydantic rejected that as an
    invalid integer, so the app died on a traceback before it could report the
    much more useful "you haven't filled this in yet".
    """
    assert getattr(Settings(**{field: ""}), field) is None
    assert getattr(Settings(**{field: "  "}), field) is None
    assert getattr(Settings(**{field: "12345"}), field) == 12345


def test_pasted_credentials_tolerate_quotes_and_whitespace():
    assert Settings(groq_api_key='  "gsk_abc"  ').groq_api_key == "gsk_abc"
    assert Settings(telegram_bot_token="'123:AAH'").telegram_bot_token == "123:AAH"


def test_missing_lists_exactly_what_still_needs_filling_in():
    assert Settings(groq_api_key="gsk_abc", telegram_bot_token="").missing() == [
        "TELEGRAM_BOT_TOKEN"
    ]
    assert Settings(groq_api_key="", telegram_bot_token="t").missing() == ["GROQ_API_KEY"]


def test_admin_id_is_not_required_to_start():
    """The seat is claimable by /start, so nobody has to look up a numeric id."""
    ready = Settings(groq_api_key="k", telegram_bot_token="t", admin_telegram_user_id="")
    assert ready.missing() == []
    assert ready.admin_telegram_user_id is None


# --- Rehearsal formatting --------------------------------------------------


def rehearsal(**kw) -> Rehearsal:
    base = dict(
        action="reply",
        reason="Answered from the knowledge base at 0.91 confidence.",
        reply_text="Payouts run every Tuesday and Friday.",
        confidence=0.91,
        covered_by_kb=True,
        matched=["payout"],
        new_terms=[],
        model_error=None,
    )
    return Rehearsal(**{**base, **kw})


def test_answer_rehearsal_shows_the_answer_and_the_numbers():
    out = _format_rehearsal(rehearsal())
    assert "would answer" in out
    assert "Payouts run every Tuesday and Friday." in out
    assert "confidence: 0.91" in out
    assert "keyword hit: payout" in out


def test_quiet_rehearsal_explains_itself_without_an_answer():
    out = _format_rehearsal(
        rehearsal(
            action="queue",
            reason="No knowledge base entry covers this question.",
            reply_text=None,
            covered_by_kb=False,
            new_terms=["creebvpn"],
        )
    )
    assert "stay quiet" in out
    assert "No knowledge base entry covers" in out
    assert "in knowledge base: no" in out
    assert "new terms: creebvpn" in out


def test_ignored_rehearsal_says_why():
    out = _format_rehearsal(
        rehearsal(
            action="ignore",
            reason="No target keyword and not question-shaped.",
            reply_text=None,
            confidence=None,
            covered_by_kb=None,
            matched=[],
        )
    )
    assert "would ignore" in out
    assert "keyword hit: none" in out


def test_rehearsal_always_says_nothing_was_stored():
    """The operator must never wonder whether a test filled their queue."""
    for action in ("reply", "queue", "ignore"):
        assert "nothing was stored" in _format_rehearsal(rehearsal(action=action))


def test_model_errors_are_surfaced_not_hidden():
    out = _format_rehearsal(rehearsal(model_error="AuthenticationError: invalid api key"))
    assert "model error" in out
    assert "invalid api key" in out


def test_rehearsal_escapes_html_from_the_model_and_the_user():
    out = _format_rehearsal(rehearsal(reply_text="<script>alert(1)</script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
