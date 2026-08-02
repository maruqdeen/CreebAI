"""Load a plain-text knowledge base and derive the keywords to go with it.

    python -m app.kb_import creebvpn.txt
    pbpaste | python -m app.kb_import -          # straight from the clipboard
    python -m app.kb_import kb.txt --yes         # no confirmation prompt

Plain text is the correct format. The knowledge base is read by a language
model, which reads prose natively — turning it into JSON makes it *harder* for
the model, not easier. The only JSON in this system is the model's reply back
to us, which is a different thing entirely.

The important half of this command is the keywords. A group message must match
a target keyword before the bot will consult the knowledge base at all, so a
new knowledge base with the old keywords still attached leaves the bot mute on
its own subject matter. This derives the keywords from the text you just wrote,
shows them to you, and lets you edit before anything is saved.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import init_db, session_scope
from app.kb import derive_keywords
from app.models import TELEGRAM, ChannelSettings, Keyword

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED = "\033[32m", "\033[33m", "\033[31m"

# Past roughly this much, the whole-knowledge-base-in-the-prompt approach starts
# costing real money per message and retrieval becomes the better design.
LARGE_KB_CHARS = 160_000


def read_source(source: str) -> str:
    if source == "-":
        print(f"{DIM}Reading from stdin — paste, then press Ctrl-D.{RESET}", file=sys.stderr)
        return sys.stdin.read()
    path = Path(source).expanduser()
    if not path.is_file():
        raise SystemExit(f"{RED}No such file: {path}{RESET}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="path to a .txt/.md file, or - for stdin")
    parser.add_argument("--channel", default=TELEGRAM)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    parser.add_argument(
        "--keep-keywords",
        action="store_true",
        help="leave the existing keywords alone and only replace the text",
    )
    parser.add_argument("--limit", type=int, default=25, help="how many keywords to derive")
    args = parser.parse_args()

    text = read_source(args.source).strip()
    if not text:
        raise SystemExit(f"{RED}That source was empty — nothing to import.{RESET}")

    init_db()
    with session_scope() as session:
        cs = session.scalar(
            select(ChannelSettings).where(ChannelSettings.channel == args.channel)
        )
        if cs is None:
            cs = ChannelSettings(channel=args.channel, kb_text="")
            session.add(cs)
            session.flush()

        existing = list(
            session.scalars(select(Keyword.term).where(Keyword.channel == args.channel))
        )
        suggested = derive_keywords(text, limit=args.limit)

        print(f"\n{BOLD}Knowledge base{RESET}")
        print(f"  {len(text):,} characters, {len(text.splitlines())} lines")
        print(f"  replacing {len(cs.kb_text or ''):,} characters currently stored")
        if len(text) > LARGE_KB_CHARS:
            print(
                f"  {YELLOW}Large. The whole thing goes into every prompt, so this will "
                f"cost real tokens per message.{RESET}"
            )

        print(f"\n{BOLD}Keywords derived from it{RESET}  {DIM}(a message must match one "
              f"of these before the bot consults the knowledge base){RESET}")
        if not suggested:
            print(f"  {YELLOW}none — the text was too short to find repeated subjects{RESET}")
        for term, count in suggested:
            print(f"  {term:<28} {DIM}seen {count}×{RESET}")

        if args.keep_keywords:
            print(f"\n{DIM}--keep-keywords: existing {len(existing)} keywords kept, "
                  f"nothing above will be added.{RESET}")
        else:
            print(f"\n  {DIM}These replace the current {len(existing)} keywords "
                  f"({', '.join(existing[:6])}{'…' if len(existing) > 6 else ''}){RESET}")

        if not args.yes:
            answer = input(f"\n{BOLD}Save?{RESET} [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                session.rollback()
                print("Nothing saved.")
                return 1

        cs.kb_text = text
        if not args.keep_keywords and suggested:
            session.query(Keyword).filter(Keyword.channel == args.channel).delete()
            for term, _ in suggested:
                session.add(Keyword(channel=args.channel, term=term))

        print(f"\n{GREEN}Saved.{RESET} Restart the bot to pick it up, then try:")
        print(f'  {DIM}.venv/bin/python -m app.simulate "your test question"{RESET}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
