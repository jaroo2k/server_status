"""Delivers queued notifications. Runs apart from collection, so a slow or unavailable
Telegram can't hold up collecting logs."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, utcnow
from app.notifier.telegram import TelegramRetryAfter

log = logging.getLogger("notifier")

POLL_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
BATCH_SIZE = 20
# Telegram's documented limit for sending to one chat
SEND_GAP_SECONDS = 1


async def send_pending(
    session: AsyncSession,
    send: Callable[[str], Awaitable[None]],
    *,
    worker_index: int,
    worker_count: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    rows = await session.scalars(
        select(Notification)
        .where(
            Notification.sent_at.is_(None),
            Notification.server_id % worker_count == worker_index,
        )
        .order_by(Notification.id)
        .limit(BATCH_SIZE)
    )
    for row in rows.all():
        await send(row.text)
        row.sent_at = utcnow()
        # per message, so a crash can't send the earlier ones a second time
        await session.commit()
        await sleep(SEND_GAP_SECONDS)


async def dispatch_forever(
    once: Callable[[], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    failures = 0
    while True:
        try:
            await once()
            failures = 0
            delay = POLL_SECONDS
        except TelegramRetryAfter as e:
            delay = e.retry_after
            log.warning("telegram rate limit, waiting %ss", delay)
        except Exception as e:  # noqa: BLE001  (undelivered rows stay queued for the next try)
            failures += 1
            delay = min(POLL_SECONDS * 2**failures, MAX_BACKOFF_SECONDS)
            log.warning("alert delivery failed, retrying in %ss: %s", delay, e)
        await sleep(delay)
