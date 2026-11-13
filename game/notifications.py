"""Transactional outgoing Telegram queue. Send only after the game transaction commits."""

import asyncio
import logging
from datetime import timedelta
from html import escape
from sqlalchemy import select
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramNetworkError,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from game.models import Notification, utcnow
from game.formatting import message_numbers


def enqueue(session, telegram_id, text, buttons=None):
    session.add(Notification(telegram_id=telegram_id, text=text, buttons=buttons or []))


async def deliver(database, bot, rules):
    count = 0
    for _ in range(rules["notifications"]["batch_size"]):
        async with database.sessions() as session, session.begin():
            now = utcnow()
            item = await session.scalar(
                select(Notification)
                .where(Notification.sent_at.is_(None), Notification.next_attempt <= now)
                .order_by(Notification.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if not item:
                break
            markup = (
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=b["text"], callback_data=b["data"]) for b in row]
                        for row in item.buttons
                    ]
                )
                if item.buttons
                else None
            )
            try:
                await bot.send_message(
                    chat_id=item.telegram_id, text=message_numbers(escape(item.text)), reply_markup=markup
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                item.sent_at = now  # User blocked bot / chat unavailable: event history remains accessible.
            except TelegramRetryAfter as exc:
                item.next_attempt = now + timedelta(seconds=exc.retry_after)
            except (TelegramNetworkError, TelegramServerError):
                item.next_attempt = now + timedelta(seconds=rules["notifications"]["retry_seconds"])
            else:
                item.sent_at = now
                count += 1
            item.attempts += 1
    return count


async def run(database, bot, rules):
    while True:
        try:
            await deliver(database, bot, rules)
        except Exception:
            logging.getLogger(__name__).exception("Notification delivery failed; queue will retry")
        await asyncio.sleep(rules["notifications"]["poll_seconds"])
