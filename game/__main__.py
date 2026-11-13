"""Run the Telegram long-polling frontend: python -m game."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from game.bot.router import make_router
from game.config import RULES, Settings
from game.formatting import configure_log_clock
from game.db import Database, SchemaNotReady
from game.notifications import run as run_notifications
from contextlib import suppress


async def main() -> None:
    settings = Settings.from_env()
    configure_log_clock()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is required. Copy .env.example to .env and configure the bot token.")
    database = Database(settings.database_url)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(database, RULES))
    notifications = None
    try:
        await database.check_schema()
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть игру"),
                BotCommand(command="menu", description="Новое меню страны"),
                BotCommand(command="rank", description="Рейтинг"),
                BotCommand(command="unlist", description="Снять свой лот: /unlist ID"),
                BotCommand(command="create", description="Основать страну: /create MyCountry"),
                BotCommand(command="cancel", description="Отменить ввод"),
                BotCommand(command="launch", description="Запустить космическую экспедицию"),
                BotCommand(command="land", description="Вернуть космическую экспедицию"),
            ]
        )
        notifications = asyncio.create_task(run_notifications(database, bot, RULES))
        await dispatcher.start_polling(
            bot, polling_timeout=RULES["ui"]["polling_timeout"], allowed_updates=dispatcher.resolve_used_update_types()
        )
    except SchemaNotReady as exc:
        raise SystemExit(str(exc)) from None
    finally:
        if notifications:
            notifications.cancel()
            with suppress(asyncio.CancelledError):
                await notifications
        await bot.session.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
