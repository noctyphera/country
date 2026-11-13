from datetime import timedelta, datetime, timezone
import random
from sqlalchemy import select, func
import pytest
from test_gameplay import create
from test_ui import FakeBot, callback, get_handler
from game.config import RULES
from game.core import GameError, spend
from game.models import (
    Country,
    Alliance,
    AllianceMember,
    AllianceInvitation,
    Notification,
    GameEvent,
    CountryWar,
    utcnow,
)
from game.services.alliance import AllianceService
from game.services.market import MarketService
from game.services.country_war import CountryWarService
from game.services.country import GameService
from game.bot.views import render_page
from game.bot.router import make_router
from game.bot.presentation import Callback
from game.notifications import deliver
from game.formatting import remaining, date_time


async def independent(session, tg=101, name="Aurora"):
    c = await create(session, tg, name)
    c.independent = True
    c.money = 1000000
    return c


async def test_invitation_only_owner_expiry_and_rejection(game_session):
    await independent(game_session)
    b = await independent(game_session, 102, "Borealis")
    svc = AllianceService(game_session, RULES)
    await svc.create(101, "Union")
    aid = (await svc.snapshot(101))["id"]
    with pytest.raises(GameError, match="приглашение"):
        await svc.join(102, aid)
    await svc.invite(101, 102)
    inv = await game_session.scalar(select(AllianceInvitation))
    with pytest.raises(GameError, match="не ваше"):
        await svc.respond(101, inv.id, True)
    assert not await game_session.get(AllianceMember, b.id)
    assert (inv.expires_at - utcnow()).total_seconds() <= 3600
    inv.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(GameError):
        await svc.respond(102, inv.id, True)
    await svc.invite(101, 102)
    newer = await game_session.scalar(select(AllianceInvitation).order_by(AllianceInvitation.id.desc()))
    await svc.respond(102, newer.id, False)
    with pytest.raises(GameError):
        await svc.respond(102, newer.id, True)
    assert (await game_session.scalar(select(Notification))).buttons[0][0]["data"].startswith("i:102:")


async def test_accept_and_leader_leave_clears_members(game_session):
    await independent(game_session)
    b = await independent(game_session, 102, "Borealis")
    svc = AllianceService(game_session, RULES)
    await svc.create(101, "Union")
    await svc.invite(101, 102)
    inv = await game_session.scalar(select(AllianceInvitation))
    await svc.respond(102, inv.id, True)
    assert await game_session.get(AllianceMember, b.id)
    with pytest.raises(GameError):
        await svc.respond(102, inv.id, True)
    await svc.leave(101)
    assert not await game_session.get(AllianceMember, b.id)
    assert await svc.snapshot(102) is None
    assert (await game_session.scalar(select(Alliance))).disbanded


async def test_sale_queues_message_and_event(game_session):
    a = await create(game_session)
    await create(game_session, 102, "Borealis")
    svc = MarketService(game_session, RULES)
    await svc.create_order(101, "wood", 20)
    order = (await svc.orders(101))[0]
    text, _ = await render_page(game_session, RULES, 101, "offers")
    assert "20" in text and "¤60 всего" in text
    await svc.buy_order(102, order["id"])
    notice = await game_session.scalar(select(Notification))
    event = await game_session.scalar(select(GameEvent).where(GameEvent.country_id == a.id))
    assert notice.telegram_id == 101 and "Доски" in notice.text and "¤60" in notice.text
    assert event.text == notice.text
    with pytest.raises(GameError):
        await svc.buy_order(102, order["id"])
    assert await game_session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_notification_delivery_once(ui_db):
    class Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = Bot()
    async with ui_db.sessions() as s, s.begin():
        s.add(Notification(telegram_id=1001, text="Тест"))
    assert await deliver(ui_db, bot, RULES) == 1
    assert await deliver(ui_db, bot, RULES) == 0
    assert len(bot.sent) == 1


async def test_war_view_only_progress_and_powers(game_session):
    a = await create(game_session)
    b = await create(game_session, 102, "Borealis")
    a.soldiers = b.soldiers = 100
    await CountryWarService(game_session, RULES, random.Random(1)).start(101)
    n = await game_session.scalar(select(Notification))
    assert n.telegram_id == 102 and "напала" in n.text
    for tg in (101, 102):
        view, _ = await render_page(game_session, RULES, tg, "army")
        assert "▱" in view and a.name in view and "Borealis" in view
        assert "Сытость" not in view and "Побед" not in view
    w = await game_session.scalar(select(CountryWar))
    w.ends_at = utcnow() - timedelta(seconds=1)
    await CountryWarService(game_session, RULES, random.Random(1)).resolve()
    view, _ = await render_page(game_session, RULES, 101, "army")
    assert "Побед: 1" in view and "Щит" not in view
    ranks = await GameService(game_session, RULES).rankings("wars")
    assert ranks[0]["wars"] == 1


async def test_rank_buttons_and_no_menu_table(ui_db):
    assert "panels" not in __import__("game.models", fromlist=["Base"]).Base.metadata.tables
    bot = FakeBot()
    handler = get_handler(make_router(ui_db, RULES), "handle_callback")
    await handler(callback(bot, Callback(1001, 999, "p", "rank").pack()), bot)
    assert "Выберите показатель" in bot.edits[-1]["text"]
    choices = [b.callback_data for r in bot.edits[-1]["reply_markup"].inline_keyboard for b in r]
    assert any("rank.wars" in d for d in choices)


async def test_resource_shortage_has_russian_label(game_session):
    c = await create(game_session)
    with pytest.raises(GameError) as exc:
        await spend(game_session, c, resources={"wood": 1000000})
    assert "Доски" in str(exc.value) and "wood" not in str(exc.value)


async def test_moscow_roundtrip_and_database_session(world):
    async with world.sessions() as s:
        c = await s.scalar(select(Country))
        assert c.created_at.utcoffset() == timedelta(hours=3)
    assert utcnow().utcoffset() == timedelta(hours=3)
    assert date_time(datetime(2026, 1, 1, 0, tzinfo=timezone.utc)) == "01.01.2026 в 03:00"
    assert "через 2 ч." in remaining(utcnow() + timedelta(hours=2, minutes=1))


async def test_leader_dissolution_cancels_running_war(game_session):
    await independent(game_session)
    svc = AllianceService(game_session, RULES)
    await svc.create(101, "Union")
    from game.models import Unit, War

    c = await game_session.scalar(select(Country).where(Country.telegram_id == 101))
    (await game_session.get(Unit, (c.id, "tank"))).amount = 100
    await svc.contribute_units(101, "tank", 100)
    await svc.start_war(101)
    await svc.leave(101)
    assert (await game_session.scalar(select(War))).status == "cancelled"
    assert await svc.snapshot(101) is None
    assert await svc.resolve_due_wars() == 0


async def test_concurrent_outbox_senders_do_not_duplicate(world):
    import asyncio

    class Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = Bot()
    async with world.sessions() as s, s.begin():
        s.add(Notification(telegram_id=101, text="Проверка"))
    result = await asyncio.gather(deliver(world, bot, RULES), deliver(world, bot, RULES))
    assert sum(result) == 1 and len(bot.sent) == 1


async def test_back_cancels_only_form_in_same_message(ui_db):
    from game.models import PendingInput

    bot = FakeBot()
    handler = get_handler(make_router(ui_db, RULES), "handle_callback")
    await handler(callback(bot, Callback(1001, 0, "a", "launch").pack()), bot)
    async with ui_db.sessions() as s:
        pending = await s.get(PendingInput, 1001)
        nonce = pending.payload["nonce"]
    await handler(callback(bot, Callback(1001, nonce, "p", "home").pack()), bot)
    async with ui_db.sessions() as s:
        assert await s.get(PendingInput, 1001) is None
    await handler(callback(bot, Callback(1001, nonce, "yes").pack()), bot)
    assert "уже выполнено" in bot.edits[-1]["text"]
