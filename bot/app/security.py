"""Admin authentication.

Deliberately dependency-free: password hashing uses `hashlib.scrypt` and
session tokens are HMAC-signed with the stdlib. A JWT library would add a
dependency and an attack surface for no gain at one-operator scale.

Nothing here ever stores or logs a plaintext password. The password lives only
in the operator's head; the server holds a scrypt hash set as an environment
variable, and `python -m app.adminpw` generates it locally.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from app.config import settings

log = logging.getLogger(__name__)

# scrypt parameters. n=2**14 is the interactive-login preset: roughly 100ms on
# a modest machine, which is slow enough to make guessing expensive and fast
# enough that a login does not feel broken.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

TOKEN_TTL_SECONDS = 60 * 60 * 12  # a working day


# --- Passwords -------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """`scrypt$n$r$p$salt$hash`, safe to paste into an environment variable."""
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash. False on anything malformed."""
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), hash_hex)


# --- Session tokens --------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64(
        hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    )


def issue_token(subject: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    payload = json.dumps(
        {"sub": subject, "exp": int(time.time()) + ttl}, separators=(",", ":")
    ).encode("utf-8")
    return f"{_b64(payload)}.{_sign(payload)}"


def read_token(token: str) -> str | None:
    """The subject if the token is valid and unexpired, else None."""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = _unb64(encoded)
    except (ValueError, TypeError):
        return None

    # Compare before parsing, so a forged payload is never even read.
    if not hmac.compare_digest(_sign(payload), signature):
        return None

    try:
        data = json.loads(payload)
    except ValueError:
        return None

    if int(data.get("exp", 0)) < time.time():
        return None
    return data.get("sub") or None


# --- Configuration ---------------------------------------------------------


def admin_configured() -> bool:
    return bool(settings.admin_username and settings.admin_password_hash)


def authenticate(username: str, password: str) -> str | None:
    """A session token, or None. Timing is levelled either way.

    When no admin is configured the answer is always None: an unconfigured
    deployment must not be an open one.
    """
    if not admin_configured():
        log.error(
            "A login was attempted but ADMIN_USERNAME / ADMIN_PASSWORD_HASH are "
            "not set. Generate a hash with `python -m app.adminpw`."
        )
        return None

    # Always run the hash, so a wrong username and a wrong password take the
    # same time and neither can be distinguished from outside.
    stored = settings.admin_password_hash
    password_ok = verify_password(password, stored)
    user_ok = hmac.compare_digest(
        (username or "").strip().lower(), settings.admin_username.strip().lower()
    )

    if user_ok and password_ok:
        return issue_token(settings.admin_username)
    return None
