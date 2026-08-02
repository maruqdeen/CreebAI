"""Load seed.yaml into the database.

    python -m app.seed            # create tables, fill anything still empty
    python -m app.seed --force    # also overwrite an edited knowledge base

Idempotent by default: it will add missing keywords and rules but will not
clobber a knowledge base or threshold you have since changed.
"""

import argparse
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BOT_DIR
from app.db import init_db, session_scope
from app.models import CHANNELS, ChannelSettings, Keyword, Rule

SEED_PATH = BOT_DIR / "seed.yaml"


def _seed_channel(session: Session, channel: str, data: dict, force: bool) -> list[str]:
    notes: list[str] = []

    cs = session.scalar(select(ChannelSettings).where(ChannelSettings.channel == channel))
    if cs is None:
        cs = ChannelSettings(channel=channel, kb_text="")
        session.add(cs)
        notes.append(f"{channel}: created settings row")

    if force or not cs.kb_text:
        new_kb = data.get("kb_text", "") or ""
        if new_kb != cs.kb_text:
            cs.kb_text = new_kb
            notes.append(f"{channel}: knowledge base set ({len(new_kb)} chars)")
    elif data.get("kb_text"):
        notes.append(f"{channel}: knowledge base left alone (already written; --force to replace)")

    if "reply_threshold" in data and (force or cs.reply_threshold is None):
        cs.reply_threshold = float(data["reply_threshold"])

    if data.get("product_terms") and (force or not cs.product_terms):
        cs.product_terms = ", ".join(str(t).strip().lower() for t in data["product_terms"])
        notes.append(f"{channel}: product scope set ({len(data['product_terms'])} terms)")
    if data.get("bot_link") and not cs.bot_link:
        cs.bot_link = data["bot_link"]

    existing_terms = {
        t.lower()
        for t in session.scalars(select(Keyword.term).where(Keyword.channel == channel))
    }
    added = 0
    for term in data.get("keywords") or []:
        term = str(term).strip().lower()
        if term and term not in existing_terms:
            session.add(Keyword(channel=channel, term=term))
            existing_terms.add(term)
            added += 1
    if added:
        notes.append(f"{channel}: +{added} keywords")

    existing_refs = {
        r for r in session.scalars(select(Rule.ref).where(Rule.channel == channel))
    }
    added = 0
    for position, rule in enumerate(data.get("rules") or []):
        ref = str(rule["ref"])
        if ref in existing_refs:
            continue
        session.add(
            Rule(
                channel=channel,
                ref=ref,
                text=rule["text"],
                triggers=rule.get("triggers", "") or "",
                position=position,
            )
        )
        added += 1
    if added:
        notes.append(f"{channel}: +{added} rules")

    return notes


def seed(force: bool = False, path: Path = SEED_PATH) -> list[str]:
    init_db()
    data = yaml.safe_load(path.read_text()) or {}

    notes: list[str] = []
    with session_scope() as session:
        for channel in CHANNELS:
            if channel in data:
                notes.extend(_seed_channel(session, channel, data[channel] or {}, force))
    return notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the knowledge base and threshold even if they have been edited",
    )
    args = parser.parse_args()

    notes = seed(force=args.force)
    for note in notes:
        print(f"  {note}")
    print("Seed complete." if notes else "Nothing to do — already seeded.")


if __name__ == "__main__":
    main()
