"""Real PostgreSQL tests for escrow, spending and FK-compatible lock ordering."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from game.config import RULES
from game.core import GameError, country
from game.models import Alliance, Base, Country, Expedition, GameEvent, Inventory, MarketOrder, TransferLog, utcnow
from game.services.country import GameService
from game.services.market import MarketService
from game.services.space import SpaceService


@pytest_asyncio.fixture
async def concurrent_db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL concurrency checks")
    schema = f"test_concurrent_{uuid4().hex}"
    bootstrap = create_async_engine(url)
    async with bootstrap.begin() as connection:
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "5000",
            }
        },
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session, session.begin():
            for tg, name in ((101, "Aurora"), (102, "Borealis"), (103, "Cygnus"), (104, "Draco")):
                await GameService(session, RULES).create(tg, name)
        yield sessions
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await bootstrap.dispose()


async def attempt(sessions, service_class, method, *args):
    try:
        async with sessions() as session, session.begin():
            await getattr(service_class(session, RULES), method)(*args)
        return True
    except GameError:
        return False


async def test_concurrent_buyers_cannot_oversell_escrow(concurrent_db):
    async with concurrent_db() as session, session.begin():
        await MarketService(session, RULES).create_order(101, "wood", 100)
        order_id = await session.scalar(select(MarketOrder.id))
        money_before = await session.scalar(select(func.sum(Country.money)))
        wood_before = await session.scalar(select(func.sum(Inventory.amount)).where(Inventory.resource == "wood"))
    outcomes = await asyncio.gather(
        attempt(concurrent_db, MarketService, "buy_order", 102, order_id, 80),
        attempt(concurrent_db, MarketService, "buy_order", 103, order_id, 80),
    )
    assert sorted(outcomes) == [False, True]
    async with concurrent_db() as session:
        assert (await session.get(MarketOrder, order_id)).amount == 20
        assert await session.scalar(select(func.sum(Country.money))) == money_before
        assert (
            await session.scalar(select(func.sum(Inventory.amount)).where(Inventory.resource == "wood"))
            == wood_before + 80
        )


async def test_opposite_direction_purchases_share_lock_order(concurrent_db):
    async with concurrent_db() as session, session.begin():
        market = MarketService(session, RULES)
        await market.create_order(101, "wood", 10)
        await market.create_order(102, "iron", 10)
        orders = list((await session.scalars(select(MarketOrder.id).order_by(MarketOrder.id))).all())
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            attempt(concurrent_db, MarketService, "buy_order", 101, orders[1], 10),
            attempt(concurrent_db, MarketService, "buy_order", 102, orders[0], 10),
        ),
        timeout=8,
    )
    assert outcomes == [True, True]


async def test_concurrent_transfers_cannot_bypass_rolling_limit(concurrent_db):
    async with concurrent_db() as session, session.begin():
        countries = (await session.scalars(select(Country))).all()
        for state in countries:
            state.created_at = utcnow() - timedelta(days=2)
    outcomes = await asyncio.gather(
        *(attempt(concurrent_db, MarketService, "transfer", 101, recipient, 2000) for recipient in (102, 103, 104))
    )
    assert sum(outcomes) == 2
    async with concurrent_db() as session:
        transferred = await session.scalar(select(func.sum(TransferLog.amount)))
        assert transferred == 4000
        sender = await country(session, 101, lock=False)
        assert sender.money == RULES["initial"]["money"] - 4000 - 2 * 60


async def test_simultaneous_landing_credits_one_expedition(concurrent_db):
    async with concurrent_db() as session, session.begin():
        state = await country(session, 101)
        state.independent = True
        session.add(
            Expedition(
                country_id=state.id,
                launched_at=utcnow() - timedelta(days=2),
                returns_at=utcnow() - timedelta(days=1),
                destroyed=False,
            )
        )
    outcomes = await asyncio.gather(
        attempt(concurrent_db, SpaceService, "claim", 101),
        attempt(concurrent_db, SpaceService, "claim", 101),
    )
    assert sorted(outcomes) == [False, True]
    async with concurrent_db() as session:
        state = await country(session, 101, lock=False)
        assert (await session.get(Inventory, (state.id, "rocket"))).amount == 1
        for key, (low, high) in RULES["space"]["rewards"].items():
            assert low <= (await session.get(Inventory, (state.id, key))).amount <= high


async def test_concurrent_name_registration_is_case_insensitive(concurrent_db):
    outcomes = await asyncio.gather(
        attempt(concurrent_db, GameService, "create", 105, "Elysium"),
        attempt(concurrent_db, GameService, "create", 106, "ELYSIUM"),
    )
    assert sorted(outcomes) == [False, True]
    async with concurrent_db() as session:
        assert await session.scalar(select(func.count()).select_from(Country).where(Country.name_key == "elysium")) == 1


async def test_country_lock_allows_alliance_notifications_fk_lock(concurrent_db):
    async with concurrent_db() as session, session.begin():
        state = await country(session, 101)
        alliance = Alliance(name="Union", name_key="union", leader_country_id=state.id, money=1000)
        session.add(alliance)
        await session.flush()
        aid, cid = alliance.id, state.id
    country_locked = asyncio.Event()
    alliance_locked = asyncio.Event()

    async def player_action():
        async with concurrent_db() as session, session.begin():
            await country(session, 101)
            country_locked.set()
            await alliance_locked.wait()
            await session.scalar(select(Alliance).where(Alliance.id == aid).with_for_update())

    async def war_notification():
        await country_locked.wait()
        async with concurrent_db() as session, session.begin():
            await session.scalar(select(Alliance).where(Alliance.id == aid).with_for_update())
            alliance_locked.set()
            session.add(GameEvent(country_id=cid, text="War resolved"))
            await session.flush()

    await asyncio.wait_for(asyncio.gather(player_action(), war_notification()), timeout=8)
