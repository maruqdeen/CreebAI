"""Run the pipeline on typed text. No Telegram, no group, no bot token.

    python -m app.simulate "when is the payout processed?"
    python -m app.simulate "how long does KYC take?" --dry-run   # no Groq call
    python -m app.simulate "i want a refund" --keep              # actually store it

By default the run is rolled back, so you can poke at real behaviour as often
as you like without filling the queue with test rows. Pass --keep to commit.
"""

import argparse
import logging
import sys

from app.config import settings
from app.db import SessionLocal, init_db
from app.groq_client import GroqJudge, Verdict
from app.kb import looks_like_question, match_keywords
from app.models import TELEGRAM
from app.pipeline import IGNORE, QUEUE, REPLY, ChannelContext, Inbound, decide, persist

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
RED, GREEN, YELLOW, BLUE = "\033[31m", "\033[32m", "\033[33m", "\033[34m"

ACTION_STYLE = {REPLY: GREEN, QUEUE: YELLOW, IGNORE: DIM}


class CannedJudge:
    """Stand-in for --dry-run: plausible verdict, no network."""

    def judge(self, message: str, kb_text: str, rules: list[str]) -> Verdict:
        return Verdict(
            answer="[dry run] The knowledge base would answer here.",
            confidence=0.80,
            covered_by_kb=True,
            topic="dry run",
            keywords=[],
            latency_ms=0,
        )


def rule(title: str = "") -> None:
    print(f"{DIM}{'─' * 4} {title} {'─' * max(0, 68 - len(title))}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", help="the group message to run through the pipeline")
    parser.add_argument("--channel", default=TELEGRAM)
    parser.add_argument(
        "--dry-run", action="store_true", help="skip the Groq call and use a canned verdict"
    )
    parser.add_argument(
        "--keep", action="store_true", help="commit the result instead of rolling back"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.dry_run and not settings.groq_configured:
        print(
            f"{RED}GROQ_API_KEY is not set.{RESET} Put it in bot/.env, or run with "
            f"--dry-run to exercise everything except the model call.",
            file=sys.stderr,
        )
        return 2

    init_db()
    judge = CannedJudge() if args.dry_run else GroqJudge()
    session = SessionLocal()

    try:
        inbound = Inbound(
            text=args.message,
            channel=args.channel,
            chat_id=-100_000_000_001,
            message_id=1,
            user_id=1,
            user_handle="@simulated_user",
        )

        ctx = ChannelContext.load(session, args.channel)
        matched = match_keywords(inbound.text, ctx.keywords)

        rule("INBOUND")
        print(f'  "{inbound.text}"')
        print(f"  channel {args.channel} · model {'(dry run)' if args.dry_run else judge.model}")
        print()

        rule("GATE")
        print(f"  target keywords configured : {len(ctx.keywords)}")
        print(f"  matched                    : {', '.join(matched) if matched else '(none)'}")
        print(f"  question-shaped            : {looks_like_question(inbound.text)}")
        if matched:
            print(f"  {BLUE}→ answer path: the model will be asked{RESET}")
        elif looks_like_question(inbound.text):
            print(f"  {BLUE}→ silent capture: logged for keyword discovery, no model call{RESET}")
        else:
            print(f"  {DIM}→ ignored: ordinary chatter{RESET}")
        print()

        decision = decide(inbound, session, judge)

        if decision.verdict is not None:
            v = decision.verdict
            rule("MODEL VERDICT")
            if not v.ok:
                print(f"  {RED}call failed: {v.error}{RESET}")
            else:
                print(f"  covered_by_kb : {v.covered_by_kb}")
                print(f"  confidence    : {v.confidence:.2f}  (threshold {ctx.settings.reply_threshold:.2f})")
                print(f"  topic         : {v.topic or '(none)'}")
                print(f"  keywords      : {', '.join(v.keywords) or '(none)'}")
                print(f"  latency       : {v.latency_ms} ms")
                if v.answer:
                    print(f"  answer        : {v.answer}")
            print()

        colour = ACTION_STYLE.get(decision.action, "")
        rule("DECISION")
        print(f"  {colour}{BOLD}{decision.action.upper()}{RESET}  {decision.reason}")
        if decision.reply_text:
            print(f"\n  {GREEN}Would post to the group:{RESET}")
            for line in decision.reply_text.splitlines():
                print(f"    {line}")
        print()

        query = persist(session, inbound, decision)

        rule("DATABASE")
        if query is None:
            print("  nothing stored — ignored messages are not our business")
        else:
            print(f"  ref        : {query.ref}")
            print(f"  state      : {query.state}")
            print(f"  draft      : {(query.draft or '(none)')[:60]}")
            kws = [f"{k.term}{'*' if k.is_new else ''}" for k in query.keywords]
            print(f"  keywords   : {', '.join(kws) or '(none)'}   {DIM}* = not in keywords or KB{RESET}")
            if decision.escalated:
                print(f"  {YELLOW}→ would DM the admin{RESET}")
        print()

        if args.keep:
            session.commit()
            print(f"{GREEN}Committed.{RESET}")
        else:
            session.rollback()
            print(f"{DIM}Rolled back (pass --keep to store it).{RESET}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
