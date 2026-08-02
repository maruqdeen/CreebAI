"""Telegram webhook.

Polling holds a connection open forever, which free hosting will not allow —
an idle instance is put to sleep and the bot goes quiet. A webhook inverts it:
Telegram POSTs here when a message arrives, and that inbound request is what
wakes the instance.

Local development stays on polling, because it needs no public URL. The mode
is chosen by whether PUBLIC_URL is set; see `Settings.use_webhook`.

Two things guard this endpoint. The path carries a secret, and Telegram echoes
`TELEGRAM_WEBHOOK_SECRET` back in a header on every delivery — an unsigned POST
to a guessed URL is rejected.
"""

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.config import settings

log = logging.getLogger(__name__)

telegram_router = APIRouter()

#: Set by main.py once the Application is built, so the route can hand updates
#: to the same handlers polling uses. There is exactly one bot per process.
_application = None


def attach(application) -> None:
    global _application
    _application = application


def _authentic(request: Request, secret_in_path: str) -> bool:
    expected = settings.telegram_webhook_secret
    if not expected:
        return False
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    return hmac.compare_digest(secret_in_path, expected) and hmac.compare_digest(
        header, expected
    )


@telegram_router.post("/telegram/webhook/{secret}", include_in_schema=False)
async def telegram_webhook(secret: str, request: Request) -> Response:
    if not _authentic(request, secret):
        # 404 rather than 403: an unauthenticated caller learns nothing about
        # whether the path was right.
        log.warning("Rejected an unauthenticated webhook delivery.")
        raise HTTPException(404, "Not found")

    if _application is None:
        # Telegram retries on 5xx, so a message arriving mid-startup is not lost.
        raise HTTPException(503, "Bot is not ready yet")

    from telegram import Update

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Body was not JSON")

    update = Update.de_json(payload, _application.bot)
    if update is None:
        return Response(status_code=200)

    try:
        await _application.process_update(update)
    except Exception:
        # Never return 5xx for a handler bug: Telegram would retry the same
        # broken update forever. The error is ours to see in the logs.
        log.exception("Handler raised on update %s", getattr(update, "update_id", "?"))

    return Response(status_code=200)
