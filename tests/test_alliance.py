from __future__ import annotations

import random
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from game.config import RULES
from game.core import GameError
from game.models import (
    Alliance,
    AllianceMember,
    AllianceInvitation,
    Base,
    Country,
    GameEvent,
    Inventory,
    Unit,
    War,
    utcnow,
)
from game.services.alliance import AllianceService


@pytest_asyncio.fixture
async def alliance_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        for identifier in range(1, 13):
            session.add(
                Country(
                    id=identifier,
                    telegram_id=1000 + identifier,
                    name=f"Nation{identifier}",
                    name_key=f"nation{identifier}",
                    independent=True,
                    money=1000000,
                    population=1000,
                    land=100,
                )
            )
        await session.flush()
        for identifier in range(1, 13):
            for resource in RULES["resources"]:
                session.add(Inventory(country_id=identifier, resource=resource, amount=100000))
            session.add(Unit(country_id=identifier, kind="tank", amount=100))
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_leadership_and_disband(alliance_db):
    async with alliance_db.begin() as session:
        svc = AllianceService(session, RULES)
        await svc.create(1001, "Aurora")
        with pytest.raises(GameError, match="существует"):
            await svc.create(1012, "aUrOrA")
        for identifier in range(2, RULES["alliance"]["max_members"] + 1):
            await svc.invite(1001, 1000 + identifier)
            invitation = await session.scalar(
                select(AllianceInvitation).where(AllianceInvitation.country_id == identifier)
            )
            await svc.respond(1000 + identifier, invitation.id, True)
        with pytest.raises(GameError, match="свободных"):
            await svc.invite(1001, 1011)
        await svc.transfer_leadership(1001, 1002)
        await svc.leave(1001)
        assert (await svc.snapshot(1002))["leader_telegram_id"] == 1002
        assert await svc.snapshot(1001) is None
        with pytest.raises(GameError, match="главе"):
            await svc.payout(1003, 1002, 1)


@pytest.mark.asyncio
async def test_real_war_freezes_assets_and_resolves_once(alliance_db):
    async with alliance_db.begin() as session:
        svc = AllianceService(session, RULES)
        await svc.create(1001, "Aurora")
        await svc.create(1002, "Borealis")
        for tg in (1001, 1002):
            await svc.contribute_units(tg, "tank", 100)
            (await session.get(Alliance, (await svc.snapshot(tg))["id"])).money = 10000
        await svc.start_war(1001)
        war = await session.scalar(select(War))
        assert war.defender_id is not None
        assert (await svc.snapshot(1002))["war"]["opponent"] == "Aurora"
        with pytest.raises(GameError, match="заморожены"):
            await svc.payout(1001, 1001, 1)
        war.ends_at = utcnow() - timedelta(seconds=1)
        await session.flush()
        svc.rng = random.Random(1)
        assert await svc.resolve_due_wars() == 1
        assert war.status == "resolved"
        assert war.winner_id == war.attacker_id
        assert war.reward == int(war.stake * RULES["alliance"]["reward_multiplier"])
        money = await session.scalar(select(func.sum(Alliance.money)))
        assert money == 20000 + war.reward
        assert await svc.resolve_due_wars() == 0
        assert await session.scalar(select(func.sum(Alliance.money))) == money
        assert await session.scalar(select(func.count()).select_from(GameEvent)) == 4
        with pytest.raises(GameError, match="Следующая|мощь"):
            await svc.start_war(1001)


@pytest.mark.asyncio
async def test_simulation_reward_and_single_member_disband(alliance_db):
    async with alliance_db.begin() as session:
        svc = AllianceService(session, RULES)
        await svc.create(1001, "Aurora")
        await svc.contribute_units(1001, "tank", 100)
        await svc.start_war(1001)
        war = await session.scalar(select(War))
        assert war.defender_id is None
        assert war.opponent_name
        war.ends_at = utcnow() - timedelta(seconds=1)
        await session.flush()
        svc.rng = random.Random(1)
        await svc.resolve_due_wars()
        assert war.reward == int(war.stake * RULES["alliance"]["reward_multiplier"])
        aid = war.attacker_id
        await svc.leave(1001)
        assert (await session.get(Alliance, aid)).disbanded
        assert not await svc.rankings()
        assert await session.scalar(select(func.count()).select_from(AllianceMember)) == 0
        # Soft-disband retains foreign-key-safe history and releases the name.
        await svc.create(1002, "Aurora")
        assert (await svc.snapshot(1002))["id"] != aid


@pytest_asyncio.fixture
async def alliance_postgres_db():
    """Real row-locking tests use an isolated schema, never existing game tables."""
    import os
    from uuid import uuid4
    from sqlalchemy.schema import CreateSchema, DropSchema

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL concurrency tests")
    schema = f"test_alliance_{uuid4().hex}"
    bootstrap = create_async_engine(url)
    async with bootstrap.begin() as connection:
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory.begin() as session:
            for identifier in range(1, 4):
                session.add(
                    Country(
                        id=identifier,
                        telegram_id=1000 + identifier,
                        name=f"Nation{identifier}",
                        name_key=f"nation{identifier}",
                        independent=True,
                        money=1000000,
                        population=1000,
                        land=100,
                    )
                )
            await session.flush()
            for identifier in range(1, 4):
                session.add(Unit(country_id=identifier, kind="tank", amount=100))
        yield factory
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await bootstrap.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_join_does_not_overfill(alliance_postgres_db):
    import asyncio
    from copy import deepcopy

    rules = deepcopy(RULES)
    rules["alliance"]["max_members"] = 2
    async with alliance_postgres_db.begin() as session:
        svc = AllianceService(session, rules)
        await svc.create(1001, "Aurora")
        await svc.invite(1001, 1002)
        await svc.invite(1001, 1003)
        invitation_ids = {1000 + i.country_id: i.id for i in await session.scalars(select(AllianceInvitation))}

    async def join(tg):
        try:
            async with alliance_postgres_db.begin() as session:
                await AllianceService(session, rules).respond(tg, invitation_ids[tg], True)
            return True
        except GameError:
            return False

    assert sorted(await asyncio.gather(join(1002), join(1003))) == [False, True]
    async with alliance_postgres_db.begin() as session:
        count = await session.scalar(select(func.count()).select_from(AllianceMember))
        assert count == 2


@pytest.mark.asyncio
async def test_postgres_competing_wars_and_workers_are_idempotent(alliance_postgres_db):
    import asyncio

    async with alliance_postgres_db.begin() as session:
        svc = AllianceService(session, RULES)
        for tg, name in [(1001, "Aurora"), (1002, "Borealis")]:
            await svc.create(tg, name)
            await svc.contribute_units(tg, "tank", 100)
            (await session.get(Alliance, (await svc.snapshot(tg))["id"])).money = 10000

    async def attack(tg):
        try:
            async with alliance_postgres_db.begin() as session:
                await AllianceService(session, RULES).start_war(tg)
            return True
        except GameError:
            return False

    assert sorted(await asyncio.gather(attack(1001), attack(1002))) == [False, True]
    async with alliance_postgres_db.begin() as session:
        assert await session.scalar(select(func.count()).select_from(War)) == 1
        war = await session.scalar(select(War))
        war.ends_at = utcnow() - timedelta(seconds=1)

    async def resolve():
        async with alliance_postgres_db.begin() as session:
            return await AllianceService(session, RULES).resolve_due_wars()

    assert sorted(await asyncio.gather(resolve(), resolve())) == [0, 1]
    async with alliance_postgres_db.begin() as session:
        assert await session.scalar(select(func.sum(Alliance.money))) == 20000 + await session.scalar(
            select(func.sum(War.reward))
        )
        assert await session.scalar(select(func.count()).select_from(GameEvent)) == 4
