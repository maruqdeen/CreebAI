"""Run the bot and the API in one process.

    python -m app.main               # both
    python -m app.main --api-only    # panel API only, no bot token needed
    python -m app.main --bot-only    # bot only

One process means a reply sent from the panel goes out over the same Telegram
connection the bot is already holding, and both halves share one SQLite file.
"""

import argparse
import asyncio
import logging

import uvicorn

from app.api import app as api_app
from app.config import settings
from app.db import init_db

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _serve(run_bot: bool, run_api: bool) -> None:
    application = None
    webhook_mode = run_bot and settings.use_webhook

    if run_bot:
        from app.telegram_bot import build_application
        from app.webhook import attach, set_registered

        application = build_application()
        await application.initialize()
        await application.start()
        me = await application.bot.get_me()

        if webhook_mode:
            # Deliveries arrive on the API's own port, so nothing extra listens.
            attach(application)
            try:
                await application.bot.set_webhook(
                    url=settings.webhook_url,
                    secret_token=settings.telegram_webhook_secret or None,
                    allowed_updates=["message"],
                    drop_pending_updates=True,
                )
                set_registered(True)
                log.info(
                    "Bot @%s on model %s — webhook registered at %s",
                    me.username,
                    settings.groq_model,
                    settings.effective_public_url + "/telegram/webhook/***",
                )
            except Exception as exc:
                # Keep serving. A crash here means no port is opened, the host
                # reports "no open ports detected", and that misleading message
                # buries the real cause. Staying up keeps the health check and
                # the panel working so the problem is visible and fixable.
                log.error(
                    "Could not register the webhook: %s. The API is still "
                    "serving, but the bot will NOT receive messages until this "
                    "is fixed and the service restarted.",
                    exc,
                )
        else:
            # Polling needs no public URL, which is what local development
            # wants. But Telegram allows one delivery method per bot, so
            # starting a poller means deleting whatever webhook is registered —
            # and if that webhook is a deployed instance, this quietly takes
            # production offline until it is redeployed. Refuse instead.
            existing = (await application.bot.get_webhook_info()).url or ""
            if existing and not settings.allow_webhook_takeover:
                host = existing.split("/telegram/webhook/")[0] or existing
                raise SystemExit(
                    f"\nA webhook is already registered for this bot:\n"
                    f"    {host}\n\n"
                    f"Polling would delete it and leave that deployment "
                    f"receiving nothing until it is redeployed.\n\n"
                    f"Either use a separate bot token for local work, or set "
                    f"ALLOW_WEBHOOK_TAKEOVER=1 to take it over deliberately.\n"
                )
            if existing:
                log.warning(
                    "Taking over the webhook registered at %s — that deployment "
                    "will receive nothing until it is redeployed.",
                    existing.split("/telegram/webhook/")[0] or existing,
                )
            await application.bot.delete_webhook(drop_pending_updates=True)
            await application.updater.start_polling(
                # Answering an hour late is worse than not answering.
                drop_pending_updates=True,
                allowed_updates=["message"],
            )
            log.info("Bot @%s is polling on model %s", me.username, settings.groq_model)

    try:
        if run_api:
            server = uvicorn.Server(
                uvicorn.Config(
                    api_app,
                    host=settings.bind_host,
                    port=settings.bind_port,
                    log_level=settings.log_level.lower(),
                    forwarded_allow_ips="*",  # behind the host's proxy
                )
            )
            await server.serve()
        else:
            # Bot only: idle until interrupted.
            await asyncio.Event().wait()
    finally:
        if application is not None:
            log.info("Stopping the bot…")
            if application.updater is not None and application.updater.running:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--api-only", action="store_true", help="serve the panel API only")
    group.add_argument("--bot-only", action="store_true", help="run the Telegram bot only")
    args = parser.parse_args()

    _configure_logging()
    init_db()

    run_bot = not args.api_only
    run_api = not args.bot_only

    if run_bot:
        gaps = settings.missing()
        if gaps:
            raise SystemExit(
                "Cannot start the bot — not set in bot/.env: "
                + ", ".join(gaps)
                + "\nRun with --api-only to start just the panel API."
            )
        if settings.use_webhook and not settings.telegram_webhook_secret:
            raise SystemExit(
                "PUBLIC_URL is set but TELEGRAM_WEBHOOK_SECRET is not, so the "
                "webhook would accept unsigned requests. Generate one with "
                "`python -m app.adminpw`."
            )

    try:
        asyncio.run(_serve(run_bot=run_bot, run_api=run_api))
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
