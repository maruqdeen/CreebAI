"""Working out the service's own public URL.

From a real deploy: a blueprint wired PUBLIC_URL with `fromService … property:
host`, which yields the service *name* ("creeb-support-bot") rather than an
address. Telegram refused it, the bot registered no webhook, and the service
looked healthy while receiving nothing.
"""

import pytest

from app.config import Settings


def settings_with(**kw) -> Settings:
    kw.setdefault("telegram_webhook_secret", "abc123")
    return Settings(**kw)


# --- What counts as a usable URL -------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        ("https://creeb.onrender.com", "https://creeb.onrender.com"),
        ("https://creeb.onrender.com/", "https://creeb.onrender.com"),
        ("http://localhost:8000", "http://localhost:8000"),
        # A bare hostname is fine — assume https, which is all Telegram accepts.
        ("creeb-support-bot.onrender.com", "https://creeb-support-bot.onrender.com"),
        ("  creeb.onrender.com  ", "https://creeb.onrender.com"),
    ],
)
def test_usable_values_become_https_urls(given, expected):
    assert settings_with(public_url=given).effective_public_url == expected


@pytest.mark.parametrize("given", ["creeb-support-bot", "myservice", "", "   "])
def test_a_service_name_is_not_a_url(given):
    """The exact failure: no dot means it is a name, not an address."""
    assert settings_with(public_url=given).effective_public_url == ""


def test_an_unusable_public_url_falls_back_to_polling_not_a_broken_webhook():
    """Better to poll than to register a webhook Telegram will refuse."""
    assert settings_with(public_url="creeb-support-bot").use_webhook is False


def test_it_is_reported_rather_than_failing_silently():
    gaps = " ".join(settings_with(public_url="creeb-support-bot").deployment_gaps())
    assert "PUBLIC_URL" in gaps
    assert "not a URL" in gaps


# --- The host's own variable -----------------------------------------------


def test_render_external_url_is_used_when_public_url_is_unset():
    s = settings_with(render_external_url="https://creeb-support-bot.onrender.com")
    assert s.effective_public_url == "https://creeb-support-bot.onrender.com"
    assert s.use_webhook is True


def test_an_explicit_public_url_wins():
    s = settings_with(
        public_url="https://bot.example.com",
        render_external_url="https://creeb.onrender.com",
    )
    assert s.effective_public_url == "https://bot.example.com"


def test_a_useless_public_url_still_falls_back_to_the_host_value():
    """The broken deploy would have recovered on its own with this."""
    s = settings_with(
        public_url="creeb-support-bot",
        render_external_url="https://creeb-support-bot.onrender.com",
    )
    assert s.effective_public_url == "https://creeb-support-bot.onrender.com"


def test_nothing_set_means_polling():
    assert settings_with().use_webhook is False


# --- The URL Telegram is given ---------------------------------------------


def test_the_webhook_url_is_absolute_and_carries_the_secret():
    s = settings_with(
        render_external_url="creeb-support-bot.onrender.com",
        telegram_webhook_secret="s3cr3t-token_A",
    )
    assert s.webhook_url == (
        "https://creeb-support-bot.onrender.com/telegram/webhook/s3cr3t-token_A"
    )


# --- CORS -------------------------------------------------------------------


def test_a_bare_panel_hostname_becomes_a_matchable_origin():
    """A browser origin without a scheme matches nothing."""
    origins = settings_with(
        extra_cors_origins="creeb-support-panel.onrender.com"
    ).cors_origins()
    assert "https://creeb-support-panel.onrender.com" in origins


def test_several_origins_are_accepted():
    origins = settings_with(
        extra_cors_origins="https://a.example.com, b.example.com"
    ).cors_origins()
    assert "https://a.example.com" in origins
    assert "https://b.example.com" in origins


def test_a_service_name_is_not_accepted_as_an_origin():
    assert "creeb-support-panel" not in settings_with(
        extra_cors_origins="creeb-support-panel"
    ).cors_origins()


def test_local_development_origins_are_always_allowed():
    assert "http://localhost:5190" in settings_with().cors_origins()
