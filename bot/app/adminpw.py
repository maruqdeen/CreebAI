"""Generate the admin password hash, locally.

    python -m app.adminpw

Prompts for a password (nothing is echoed), prints the hash to paste into
ADMIN_PASSWORD_HASH, and also prints a fresh SECRET_KEY and
TELEGRAM_WEBHOOK_SECRET.

The password itself is never stored, transmitted, or written to a file. Run
this on your own machine and copy the hash into your host's environment
settings — not into the repository.
"""

import getpass
import secrets
import sys

from app.security import hash_password

BOLD, DIM, RESET, RED = "\033[1m", "\033[2m", "\033[0m", "\033[31m"


def main() -> int:
    print(f"\n{BOLD}Admin password{RESET}")
    print(f"{DIM}Nothing is echoed. The password is not saved anywhere.{RESET}\n")

    password = getpass.getpass("  Password: ")
    if len(password) < 10:
        print(f"\n{RED}Too short — use at least 10 characters.{RESET}", file=sys.stderr)
        return 1

    if password != getpass.getpass("  Confirm:  "):
        print(f"\n{RED}They do not match.{RESET}", file=sys.stderr)
        return 1

    print(f"\n{BOLD}Set these in Render → Environment{RESET}")
    print(f"{DIM}(and in bot/.env if you want the same login locally){RESET}\n")
    print(f"ADMIN_USERNAME=admin")
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}")
    print(f"SECRET_KEY={secrets.token_urlsafe(32)}")
    print(f"TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(24)}")
    print(
        f"\n{DIM}Change ADMIN_USERNAME if you want something other than "
        f"'admin'. Changing SECRET_KEY later logs you out everywhere, which is "
        f"how you revoke a session.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
