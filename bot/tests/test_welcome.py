"""Greeting people who join the group."""

import pytest

from app.flows import load_flows, render_welcome
from app.telegram_bot import display_name

TEMPLATE = "Hi {name}\nWelcome to the creebspace community I'm your Ai assistant!"


class FakeUser:
    def __init__(self, username=None, full_name="", is_bot=False):
        self.username = username
        self.full_name = full_name
        self.is_bot = is_bot


# --- How someone is addressed ----------------------------------------------


def test_a_handle_is_preferred_because_it_notifies_them():
    assert display_name(FakeUser(username="MayorRuffie")) == "@MayorRuffie"


def test_someone_without_a_handle_gets_their_name():
    assert display_name(FakeUser(full_name="Chidi Okonkwo")) == "Chidi Okonkwo"


def test_someone_with_neither_still_gets_greeted():
    """A blank greeting reads worse than a generic one."""
    assert display_name(FakeUser()) == "there"


# --- The message -----------------------------------------------------------


def test_one_person():
    assert render_welcome(TEMPLATE, ["@ada"]).startswith("Hi @ada")


def test_two_people_joining_together_share_one_message():
    assert render_welcome(TEMPLATE, ["@ada", "@tunde"]).startswith("Hi @ada and @tunde")


def test_three_or_more_read_as_a_list():
    text = render_welcome(TEMPLATE, ["@a", "@b", "@c"])
    assert text.startswith("Hi @a, @b and @c")


def test_the_body_survives_intact():
    assert "creebspace community" in render_welcome(TEMPLATE, ["@ada"])


def test_an_empty_template_greets_nobody():
    """Blank is how an operator turns the greeting off."""
    assert render_welcome("", ["@ada"]) == ""
    assert render_welcome("   ", ["@ada"]) == ""


def test_nobody_to_greet_produces_nothing():
    assert render_welcome(TEMPLATE, []) == ""


def test_operator_copy_may_contain_braces_and_percent_signs():
    """Replacement, not str.format — their wording must never raise."""
    odd = "Hi {name} — 100% welcome {see the pinned post}"
    assert render_welcome(odd, ["@ada"]) == "Hi @ada — 100% welcome {see the pinned post}"


# --- Configuration ---------------------------------------------------------


def test_the_shipped_copy_is_the_operators_wording():
    text = load_flows().reply("welcome")
    assert "{name}" in text, "the placeholder must survive editing"
    assert "creebspace community" in text
    assert "Ai assistant" in text


def test_the_shipped_copy_renders():
    rendered = render_welcome(load_flows().reply("welcome"), ["@MayorRuffie"])
    assert rendered.startswith("Hi @MayorRuffie")
    assert "{name}" not in rendered
