"""Regression coverage for the revised game contracts."""

from datetime import timedelta, datetime, timezone
from copy import deepcopy
import math
import random
import pytest
from sqlalchemy import select
from test_gameplay import create
from test_worker import snapshot
from game.config import RULES
from game.core import GameError
from game.models import Country, CountryWar, Inventory, Unit, Disaster, utcnow
from game.services.country import GameService
from game.services.market import MarketService
from game.services.country_war import CountryWarService
from game.bot.views import render_page
from game.bot.presentation import number, money, inventory_lines
from game.ticks import disaster_tick, apply_disaster


async def test_private_unit_offer_is_reserved_and_returned(game_session):
    a = await create(game_session)
    await create(game_session, 102, "Borealis")
    unit = await game_session.get(Unit, (a.id, "tank"))
    unit.amount = 10
    market = MarketService(game_session, RULES)
    await market.create_order(101, "tank", 4)
    assert unit.amount == 6
    assert not await market.orders(102)
    offers = await market.orders(101)
    oid = offers[0]["id"]
    await market.cancel_order(101, oid)
    assert unit.amount == 10
    with pytest.raises(GameError):
        await market.cancel_order(101, oid)
    assert unit.amount == 10
    await market.create_order(101, "tank", 3)
    oid = (await market.orders(101))[0]["id"]
    await market.buy_order(102, oid)
    b = await game_session.scalar(select(Country).where(Country.telegram_id == 102))
    assert (await game_session.get(Unit, (b.id, "tank"))).amount == 3
    assert unit.amount == 7
    assert not await market.orders(101)


async def test_twenty_offer_limit(game_session):
    await create(game_session)
    svc = MarketService(game_session, RULES)
    for _ in range(20):
        await svc.create_order(101, "wood", 1)
    with pytest.raises(GameError):
        await svc.create_order(101, "wood", 1)


async def test_food_is_allocated_from_warehouse(game_session):
    c = await create(game_session)
    c.hunger = 50
    c.army_hunger = 50
    c.soldiers = 100
    food = await game_session.get(Inventory, (c.id, "food"))
    old = food.amount
    await GameService(game_session, RULES).feed_population(101, 20)
    assert c.hunger == 60 and food.amount == old - 20
    await GameService(game_session, RULES).feed(101, 30)
    assert c.army_hunger == 60 and food.amount == old - 50


async def test_hidden_sections_and_resource_order(game_session):
    c = await create(game_session)
    text, rows = await render_page(game_session, RULES, 101, "home")
    destinations = [button[2] for row in rows for button in row]
    assert not {"energy", "space", "alliance", "market", "intel", "rank", "help"} & set(destinations)
    assert "Сытость" not in text and "Военнослужащие" not in text
    warehouse, _ = await render_page(game_session, RULES, 101, "warehouse")
    assert warehouse.index("Доски") < warehouse.index("Железо")
    production, _ = await render_page(game_session, RULES, 101, "production")
    assert "+500" in production
    c.independent = True
    _, rows = await render_page(game_session, RULES, 101, "home")
    assert "energy" in [b[2] for r in rows for b in r]


def test_consistent_number_and_inventory_order():
    assert number(941278330) == "941.278.330"
    assert money(941278330) == "¤941.278.330"
    assert inventory_lines({"iron": 1, "wood": 2}, RULES).index("Доски") < inventory_lines(
        {"iron": 1, "wood": 2}, RULES
    ).index("Железо")


async def test_country_war_shield_loot_and_once_only(game_session):
    a = await create(game_session)
    b = await create(game_session, 102, "Borealis")
    a.soldiers = b.soldiers = 100
    service = CountryWarService(game_session, RULES, random.Random(1))
    await service.start(101)
    w = await game_session.scalar(select(CountryWar))
    w.ends_at = utcnow() - timedelta(seconds=1)
    initial = b.money
    assert await service.resolve() == 1
    assert w.winner_id == a.id and b.shield_until and a.war_ready_at
    assert b.money == initial - math.floor(initial * RULES["country_war"]["loot_share"])
    assert await service.resolve() == 0
    with pytest.raises(GameError):
        await service.start(101)


@pytest.mark.parametrize("day", [(2026, 3, 8), (2026, 11, 1)])
async def test_dst_disaster_stays_in_calendar_day(world, day):
    from zoneinfo import ZoneInfo

    class LastSecond:
        def randrange(self, n):
            return n - 1

        def choice(self, choices):
            return "fire"

        def uniform(self, a, b):
            return a

    rules = deepcopy(RULES)
    rules["disasters"]["timezone"] = "America/New_York"
    at = datetime(*day, 12, tzinfo=ZoneInfo("America/New_York"))
    async with world.sessions() as session, session.begin():
        await disaster_tick(session, rules, at, LastSecond())
        d = await session.get(Disaster, at.date().isoformat())
        assert d.scheduled_at.astimezone(ZoneInfo("America/New_York")).date() == at.date()


async def test_past_disaster_does_not_hit_country_created_later(world):
    before = await snapshot(world)
    async with world.sessions() as session, session.begin():
        c = await session.get(Country, before["id"])
        c.independent = True
        d = Disaster(
            day="2020-01-01", scheduled_at=datetime(2020, 1, 1, tzinfo=timezone.utc), kind="virus", damage=0.03
        )
        session.add(d)
        await session.flush()
        await apply_disaster(session, RULES, d, utcnow())
    assert (await snapshot(world))["population"] == before["population"]


async def test_postgres_country_war_resolution_is_concurrent_and_idempotent(world):
    import asyncio
    from sqlalchemy import func

    async with world.sessions() as session, session.begin():
        for c in await session.scalars(select(Country)):
            c.soldiers = 100
        await CountryWarService(session, RULES).start(101)
        war = await session.scalar(select(CountryWar))
        war.ends_at = utcnow() - timedelta(seconds=1)
        total = await session.scalar(select(func.sum(Country.money)))

    async def resolve():
        async with world.sessions() as session, session.begin():
            return await CountryWarService(session, RULES).resolve()

    assert sorted(await asyncio.gather(resolve(), resolve())) == [0, 1]
    async with world.sessions() as session:
        assert await session.scalar(select(func.sum(Country.money))) == total
        assert (await session.scalar(select(CountryWar))).resolved_at is not None


def test_message_formats_keep_ids_and_dates():
    from game.formatting import message_numbers

    assert (
        message_numbers("Баланс ¤1234567; лот №1234567; ID 1234567; <code>1234567</code>; 12.11.2026 в 21:00")
        == "Баланс ¤1.234.567; лот №1234567; ID 1234567; <code>1234567</code>; 12.11.2026 в 21:00"
    )


async def test_postgres_random_intelligence_matching(world):
    from game.services.intelligence import IntelligenceService

    async with world.sessions() as session, session.begin():
        for c in await session.scalars(select(Country)):
            c.created_at = utcnow() - timedelta(days=2)
        assert await IntelligenceService(session, RULES).random_target(101) == 102


async def test_warring_country_cannot_escrow_its_equipment(game_session):
    a = await create(game_session)
    b = await create(game_session, 102, "Borealis")
    a.soldiers = b.soldiers = 100
    (await game_session.get(Unit, (a.id, "tank"))).amount = 1
    await CountryWarService(game_session, RULES).start(101)
    with pytest.raises(GameError):
        await MarketService(game_session, RULES).create_order(101, "tank", 1)
