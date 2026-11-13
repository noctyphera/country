from __future__ import annotations

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select

from game.bot.presentation import Callback, keyboard
from game.bot.router import make_router, parse_input
from game.bot.views import render_page
from game.config import RULES
from game.core import GameError
from game.db import Database
from game.models import Base, Country, Inventory, PendingInput, utcnow
from game.services.country import GameService


class FakeBot:
    def __init__(self):
        self.answers = []
        self.edits = []

    async def __call__(self, method, **kwargs):
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
        return True

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True


def message(bot, user_id=1001, text="1", message_id=50):
    return Message(
        message_id=message_id,
        date=utcnow(),
        chat=Chat(id=1001, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Player"),
        text=text,
    ).as_(bot)


def callback(bot, data, user_id=1001):
    return CallbackQuery(
        id="test-callback",
        from_user=User(id=user_id, is_bot=False, first_name="Player"),
        chat_instance="test",
        data=data,
        message=message(bot, message_id=20),
    ).as_(bot)


def get_handler(router, name):
    return next(
        handler.callback
        for observer in (router.message, router.callback_query)
        for handler in observer.handlers
        if handler.callback.__name__ == name
    )


@pytest_asyncio.fixture
async def ui_db():
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.sessions() as session, session.begin():
        await GameService(session, RULES).create(1001, "Auroria")
        await GameService(session, RULES).create(1002, "Borealia")
    yield db
    await db.close()


def test_callback_and_input_boundaries():
    assert Callback.unpack(Callback(1001, 123, "p", "army").pack()).owner == 1001
    assert parse_input("1234567890", "id", RULES) == 1234567890
    for value in ("-1", "0", "1.5"):
        with pytest.raises(GameError):
            parse_input(value, "int", RULES)


async def test_foreign_owner_rejected_without_database():
    class NoDatabase:
        def sessions(self):
            raise AssertionError("Should not access database")

    bot = FakeBot()
    handler = get_handler(make_router(NoDatabase(), RULES), "handle_callback")
    await handler(callback(bot, Callback(1001, 0, "p", "army").pack(), 1002), bot)
    assert "не ваша" in bot.answers[0].text


async def test_all_views_render_without_tracking_menus(ui_db):
    async with ui_db.sessions() as session, session.begin():
        for page in (
            "home",
            "warehouse",
            "market",
            "offers",
            "production",
            "population",
            "army",
            "intel",
            "rank",
            "rank.money",
            "rank.gdp",
            "rank.wars",
            "events",
        ):
            text, rows = await render_page(session, RULES, 1001, page)
            assert text and rows
            keyboard(1001, 0, rows)
        c = await session.scalar(select(Country).where(Country.telegram_id == 1001))
        c.independent = True
        for page in ("energy", "space", "alliance"):
            text, rows = await render_page(session, RULES, 1001, page)
            assert text and rows


async def test_forms_reject_duplicate_confirmation(ui_db):
    bot = FakeBot()
    router = make_router(ui_db, RULES)
    handler = get_handler(router, "handle_callback")
    async with ui_db.sessions() as session, session.begin():
        c = await session.scalar(select(Country).where(Country.telegram_id == 1001))
        c.independent = True
        c.money = 1000000
        for r in await session.scalars(select(Inventory).where(Inventory.country_id == c.id)):
            if r.resource != "rocket":
                r.amount = 100000
    await handler(callback(bot, Callback(1001, 0, "a", "rocket").pack()), bot)
    async with ui_db.sessions() as session:
        nonce = (await session.get(PendingInput, 1001)).payload["nonce"]
    await handler(callback(bot, Callback(1001, nonce, "yes").pack()), bot)
    await handler(callback(bot, Callback(1001, nonce, "yes").pack()), bot)
    async with ui_db.sessions() as session:
        c = await session.scalar(select(Country).where(Country.telegram_id == 1001))
        assert (await session.get(Inventory, (c.id, "rocket"))).amount == 1
    assert "уже выполнено" in bot.edits[-1]["text"]
