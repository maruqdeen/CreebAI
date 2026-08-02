"""Admin authentication.

The panel reads every message your users send and can rewrite the knowledge
base the bot answers from. Deployed without this, anyone who found the URL
could do both.
"""

import time

import pytest
from conftest import make_client

from app.config import settings
from app.security import (
    authenticate,
    hash_password,
    issue_token,
    read_token,
    verify_password,
)

PASSWORD = "correct horse battery staple"


@pytest.fixture
def admin(monkeypatch):
    """A configured administrator, as production would have."""
    monkeypatch.setattr(settings, "admin_username", "creebadmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_password(PASSWORD))
    monkeypatch.setattr(settings, "secret_key", "test-secret-key")


@pytest.fixture
def locked(session, monkeypatch, admin):
    """A client with the real guard in place — nobody is signed in."""
    return make_client(session, monkeypatch, signed_in=False)


# --- Password hashing ------------------------------------------------------


def test_a_password_round_trips():
    stored = hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored) is True
    assert verify_password("wrong", stored) is False


def test_the_hash_never_contains_the_password():
    assert PASSWORD not in hash_password(PASSWORD)


def test_every_hash_is_salted_differently():
    """Two operators with the same password must not share a hash."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


@pytest.mark.parametrize(
    "stored", ["", "not-a-hash", "scrypt$bad", "md5$1$2$3$4$5", "scrypt$x$8$1$aa$bb"]
)
def test_malformed_stored_hashes_reject_rather_than_crash(stored):
    assert verify_password(PASSWORD, stored) is False


def test_an_empty_password_is_never_accepted():
    assert verify_password("", hash_password(PASSWORD)) is False


# --- Session tokens --------------------------------------------------------


def test_a_token_round_trips(admin):
    assert read_token(issue_token("creebadmin")) == "creebadmin"


def test_an_expired_token_is_refused(admin):
    assert read_token(issue_token("creebadmin", ttl=-1)) is None


def test_a_tampered_payload_is_refused(admin):
    """The signature is checked before the payload is even parsed."""
    token = issue_token("creebadmin")
    payload, signature = token.rsplit(".", 1)
    forged = issue_token("someone-else").rsplit(".", 1)[0]
    assert read_token(f"{forged}.{signature}") is None


def test_a_token_signed_with_another_key_is_refused(admin, monkeypatch):
    token = issue_token("creebadmin")
    monkeypatch.setattr(settings, "secret_key", "a-different-key")
    assert read_token(token) is None, "rotating SECRET_KEY must revoke sessions"


@pytest.mark.parametrize("token", ["", "rubbish", "a.b", "....", "eyJ9.x"])
def test_garbage_tokens_reject_rather_than_crash(token, admin):
    assert read_token(token) is None


# --- Logging in ------------------------------------------------------------


def test_the_right_credentials_return_a_token(admin):
    assert read_token(authenticate("creebadmin", PASSWORD)) == "creebadmin"


def test_the_username_is_case_insensitive(admin):
    assert authenticate("CreebAdmin", PASSWORD) is not None


@pytest.mark.parametrize(
    "user, password",
    [("creebadmin", "wrong"), ("someone", PASSWORD), ("", ""), ("someone", "wrong")],
)
def test_wrong_credentials_return_nothing(admin, user, password):
    assert authenticate(user, password) is None


def test_an_unconfigured_deployment_is_closed_not_open(monkeypatch):
    """The dangerous failure would be treating "no admin set" as "no auth"."""
    monkeypatch.setattr(settings, "admin_username", "")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    assert authenticate("anyone", "anything") is None


# --- The endpoints ---------------------------------------------------------


def test_login_returns_a_usable_token(locked):
    response = locked.post(
        "/api/auth/login", json={"username": "creebadmin", "password": PASSWORD}
    )
    assert response.status_code == 200

    token = response.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert locked.get("/api/queries", headers=headers).status_code == 200


def test_login_rejects_a_wrong_password(locked):
    response = locked.post(
        "/api/auth/login", json={"username": "creebadmin", "password": "nope"}
    )
    assert response.status_code == 401


def test_the_failure_message_does_not_say_which_half_was_wrong(locked):
    wrong_user = locked.post(
        "/api/auth/login", json={"username": "nobody", "password": PASSWORD}
    ).json()["detail"]
    wrong_pass = locked.post(
        "/api/auth/login", json={"username": "creebadmin", "password": "nope"}
    ).json()["detail"]
    assert wrong_user == wrong_pass


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/queries"),
        ("get", "/api/dashboard"),
        ("get", "/api/settings/telegram"),
        ("put", "/api/settings/telegram"),
        ("get", "/api/keywords/new"),
        ("post", "/api/queries/TG-0001/reply"),
        ("post", "/api/queries/TG-0001/redraft"),
    ],
)
def test_every_protected_endpoint_refuses_an_anonymous_caller(locked, method, path):
    call = getattr(locked, method)
    response = call(path, json={}) if method in ("post", "put") else call(path)
    assert response.status_code == 401, f"{method.upper()} {path} was reachable"


def test_health_stays_open_so_the_host_can_probe_it(locked):
    body = locked.get("/api/health").json()
    assert body["ok"] is True
    # It must not leak configuration, only whether things are configured.
    assert "secret" not in str(body).lower()
    assert body["admin_configured"] is True


def test_a_bad_token_is_refused(locked):
    response = locked.get("/api/queries", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


def test_an_expired_session_is_refused(locked, admin):
    stale = issue_token("creebadmin", ttl=-1)
    response = locked.get("/api/queries", headers={"Authorization": f"Bearer {stale}"})
    assert response.status_code == 401


def test_auth_me_reports_the_signed_in_user(locked):
    token = locked.post(
        "/api/auth/login", json={"username": "creebadmin", "password": PASSWORD}
    ).json()["token"]
    body = locked.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["username"] == "creebadmin"
