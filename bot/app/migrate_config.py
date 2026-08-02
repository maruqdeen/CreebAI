"""Move the bot's configuration from one database to another.

    python -m app.migrate_config --to "postgresql://…"           # dry run
    python -m app.migrate_config --to "postgresql://…" --apply

Copies what makes the bot itself: the knowledge base, target keywords, product
scope, rules, filed answers and channel settings.

Deliberately does NOT copy queries or conversations. Those are traffic, not
configuration — carrying local test messages into production would make the
dashboard's first figures a lie and put stale rows in the queue.

The destination is not stored anywhere. Pass it on the command line or set
TARGET_DATABASE_URL; it is never written to a file.
"""

import argparse
import os
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import BOT_DIR
from app.db import _normalise
from app.models import Base, ChannelSettings, KBEntry, Keyword, Rule

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED = "\033[32m", "\033[33m", "\033[31m"

#: Copied verbatim. `ref_counter` is included so production does not reissue
#: reference numbers that already exist in your own records.
SETTINGS_FIELDS = (
    "bot_link",
    "group_chat_id",
    "kb_text",
    "reply_threshold",
    "product_terms",
    "digest_to",
    "digest_weekly",
    "digest_monthly",
    "ref_counter",
)


def _summarise(session: Session) -> dict:
    return {
        "channels": session.scalars(select(ChannelSettings)).all(),
        "keywords": session.scalars(select(Keyword)).all(),
        "rules": session.scalars(select(Rule)).all(),
        "entries": session.scalars(select(KBEntry)).all(),
    }


#: Where the configuration is coming from. Deliberately NOT DATABASE_URL:
#: by the time you migrate, that already points at the destination, and reading
#: the source from it would quietly copy the empty new database over itself.
DEFAULT_SOURCE = f"sqlite:///{BOT_DIR / 'support.db'}"


def migrate(target_url: str, apply: bool, source_url: str = DEFAULT_SOURCE) -> int:
    if _normalise(source_url) == _normalise(target_url):
        print(
            f"{RED}Source and destination are the same database.{RESET} "
            f"Pass --from to say where the configuration is coming from.",
            file=sys.stderr,
        )
        return 2

    source_engine = create_engine(source_url, future=True)
    target_engine = create_engine(_normalise(target_url), future=True, pool_pre_ping=True)

    SourceSession = sessionmaker(bind=source_engine, future=True)
    TargetSession = sessionmaker(bind=target_engine, future=True)

    with SourceSession() as source:
        data = _summarise(source)

        print(f"\n{BOLD}From{RESET} {source_url.split('@')[-1]}")
        for cs in data["channels"]:
            kb = len(cs.kb_text or "")
            scope = cs.product_term_list()
            print(f"  {cs.channel}")
            print(f"    knowledge base   {kb:,} characters")
            print(f"    reply threshold  {cs.reply_threshold}")
            print(f"    product scope    {', '.join(scope) if scope else '(none)'}")
        print(f"  {len(data['keywords'])} keywords, {len(data['rules'])} rules, "
              f"{len(data['entries'])} filed answers")
        print(f"\n{DIM}  Queries and conversations are not copied — production "
              f"starts with a clean queue.{RESET}")

        if not apply:
            print(f"\n{YELLOW}Dry run.{RESET} Re-run with --apply to write.\n")
            return 0

        print(f"\n{BOLD}To{RESET} {target_url.split('@')[-1]}")
        Base.metadata.create_all(target_engine)
        print(f"  {GREEN}✓{RESET} schema created")

        with TargetSession() as target:
            # Idempotent: running twice must not double the keyword list or
            # stack up duplicate rules.
            for cs in data["channels"]:
                existing = target.scalar(
                    select(ChannelSettings).where(ChannelSettings.channel == cs.channel)
                )
                if existing is None:
                    existing = ChannelSettings(channel=cs.channel)
                    target.add(existing)
                for field in SETTINGS_FIELDS:
                    setattr(existing, field, getattr(cs, field))

            target.query(Keyword).delete()
            for kw in data["keywords"]:
                target.add(Keyword(channel=kw.channel, term=kw.term))

            target.query(Rule).delete()
            for rule in data["rules"]:
                target.add(
                    Rule(
                        channel=rule.channel,
                        ref=rule.ref,
                        text=rule.text,
                        triggers=rule.triggers,
                        position=rule.position,
                        active=rule.active,
                    )
                )

            # Filed answers are knowledge, matched on title so a re-run tops up
            # rather than duplicating.
            titles = {t for t in target.scalars(select(KBEntry.title))}
            added = 0
            for entry in data["entries"]:
                if entry.title not in titles:
                    target.add(
                        KBEntry(
                            channel=entry.channel,
                            title=entry.title,
                            body=entry.body,
                            source=entry.source,
                        )
                    )
                    added += 1

            target.commit()

        print(f"  {GREEN}✓{RESET} settings, {len(data['keywords'])} keywords, "
              f"{len(data['rules'])} rules, {added} new filed answers")
        print(f"\n{GREEN}Done.{RESET} The deployed bot will read this on its next start.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--to",
        default=os.environ.get("TARGET_DATABASE_URL", ""),
        help="destination URL, e.g. the Neon connection string",
    )
    parser.add_argument(
        "--from",
        dest="source",
        default=DEFAULT_SOURCE,
        help="where the configuration comes from (default: the local support.db)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually write (default is a dry run)"
    )
    args = parser.parse_args()

    if not args.to:
        print(
            f"{RED}No destination.{RESET} Pass --to \"postgresql://…\" or set "
            f"TARGET_DATABASE_URL.",
            file=sys.stderr,
        )
        return 2

    return migrate(args.to, args.apply, args.source)


if __name__ == "__main__":
    raise SystemExit(main())
