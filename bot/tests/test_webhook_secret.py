"""The Telegram webhook secret.

From a real deploy: Render's `generateValue` emits base64, Telegram rejected it
with "Secret token contains unallowed characters", and because that happened
before uvicorn bound a port the host reported the far less useful "No open
ports detected".
"""

import pytest

from app.config import Settings


def secret_of(raw: str) -> str:
    return Settings(telegram_webhook_secret=raw).telegram_webhook_secret


ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


@pytest.mark.parametrize(
    "raw",
    [
        "aGVsbG8gd29ybGQ=",           # base64 with padding
        "abc+def/ghi=",               # base64 with + and /
        "with spaces in it",
        "sym!@#$%^&*()bols",
        "new\nline\ttab",
        "quotes\"and'apostrophes",
        "unicode–dash–here",
    ],
)
def test_only_characters_telegram_accepts_survive(raw):
    """Telegram allows A-Z a-z 0-9 _ - and nothing else."""
    assert set(secret_of(raw)) <= ALLOWED


def test_a_base64_value_from_the_host_still_yields_a_usable_secret():
    """Render generates base64; filtering must not leave it empty or trivial."""
    cleaned = secret_of("K7x+Qm/9zR2aB4cD8eF1gH3jK5lM7nP9qR==")
    assert len(cleaned) >= 16
    assert set(cleaned) <= ALLOWED


def test_a_slash_never_survives_into_the_url_path():
    """The same value is a path segment — a `/` would break routing outright."""
    settings = Settings(telegram_webhook_secret="abc/def")
    assert "/" not in settings.telegram_webhook_secret
    assert settings.webhook_path.count("/") == 3  # /telegram/webhook/<secret>


def test_an_already_clean_secret_is_untouched():
    clean = "aB3-x_9Zq7"
    assert secret_of(clean) == clean


def test_the_length_limit_is_respected():
    """Telegram caps secret_token at 256 characters."""
    assert len(secret_of("a" * 500)) == 256


def test_a_blank_secret_stays_blank():
    """Blank means "not configured", which main.py refuses to start on."""
    assert secret_of("") == ""
    assert secret_of("!!!") == "", "nothing usable left is the same as unset"


def test_registration_and_verification_read_the_same_value():
    """Both sides must agree, or every delivery is rejected as unauthenticated."""
    settings = Settings(telegram_webhook_secret="abc+def/ghi=")
    cleaned = settings.telegram_webhook_secret
    assert settings.webhook_path.endswith(cleaned)
