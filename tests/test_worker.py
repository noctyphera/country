"""Production SQL and scheduling tests require a real PostgreSQL test server."""

from __future__ import annotations
import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from game.config import RULES
from game.models import Base, Building, Country, Disaster, Inventory, Job, Unit, utcnow
from game.services.country import GameService
from game.ticks import apply_disaster, income_tick, production_tick
from game.worker import run_once, seed_jobs


@pytest_asyncio.fixture
async def world():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for real PostgreSQL world SQL tests")
    schema = f"test_world_{uuid4().hex}"
    bootstrap = create_async_engine(url)
    async with bootstrap.begin() as connection:
        await connection.execute(CreateSchema(schema))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})

    class World:
        sessions = async_sessionmaker(engine, expire_on_commit=False)

    database = World()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with database.sessions() as session, session.begin():
            await GameService(session, RULES).create(101, "Aurora")
            await GameService(session, RULES).create(102, "Borealis")
        yield database
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await bootstrap.dispose()


async def snapshot(database, tg=101):
    async with database.sessions() as session:
        return await GameService(session, RULES).snapshot(tg)


async def test_production_food_growth_and_housing_cap(world):
    before = await snapshot(world)
    async with world.sessions() as session, session.begin():
        await production_tick(session, RULES, utcnow())
    after = await snapshot(world)
    assert after["resources"]["wood"] - before["resources"]["wood"] == 500
    assert after["resources"]["food"] == before["resources"]["food"] + 360
    assert before["population"] < after["population"] <= after["housing_capacity"]
    async with world.sessions() as session, session.begin():
        c = await session.get(Country, before["id"])
        c.population = after["housing_capacity"]
        await session.flush()
        await production_tick(session, RULES, utcnow())
    assert (await snapshot(world))["population"] == after["housing_capacity"]


async def test_hunger_army_rations_and_deaths(world):
    initial = await snapshot(world)
    async with world.sessions() as session, session.begin():
        await session.execute(text("UPDATE buildings SET amount=0 WHERE kind IN ('farm','well')"))
        await session.execute(text("UPDATE inventory SET amount=0 WHERE resource IN ('food','water')"))
        await session.execute(text("UPDATE countries SET hunger=10, army_hunger=10, soldiers=20, spies=5, army_food=0"))
        await production_tick(session, RULES, utcnow())
    starved = await snapshot(world)
    assert starved["population"] < initial["population"]
    assert starved["soldiers"] < 20 and starved["spies"] < 5
    assert starved["hunger"] == 0 and starved["army_hunger"] == 0
    async with world.sessions() as session, session.begin():
        await session.execute(text("UPDATE countries SET army_food=1000, army_hunger=80"))
        await production_tick(session, RULES, utcnow())
    fed = await snapshot(world)
    assert fed["army_hunger"] > 80 and fed["soldiers"] == starved["soldiers"]
    assert fed["army_food"] == 1000 - (starved["soldiers"] + starved["spies"]) * RULES["economy"]["food_per_soldier"]


async def test_tax_energy_fuel_and_wear(world):
    state = await snapshot(world)
    async with world.sessions() as session, session.begin():
        c = await session.get(Country, state["id"])
        c.independent = True
        (await session.get(Building, (c.id, "solar"))).amount = 2
        (await session.get(Building, (c.id, "nuclear"))).amount = 3
        (await session.get(Inventory, (c.id, "uranium"))).amount = 4
        await session.flush()
        await income_tick(session, RULES, utcnow())
    result = await snapshot(world)
    expected = 2 * RULES["energy"]["solar_income"] + 2 * RULES["energy"]["nuclear_income"] + state["tax_income"]
    assert result["money"] - state["money"] == expected
    assert result["resources"]["uranium"] == 0
    assert result["energy_condition"] == 99
    dependent = await snapshot(world, 102)
    assert dependent["money"] == RULES["initial"]["money"] + dependent["tax_income"]


@pytest.mark.parametrize("kind", ["virus", "meteor", "aliens", "fire", "earthquake"])
async def test_disasters_small_global_independent_only_and_idempotent(world, kind):
    async with world.sessions() as session, session.begin():
        c = await session.scalar(select(Country).where(Country.telegram_id == 101))
        c.independent = True
        c.soldiers = 100
        c.spies = 100
        (await session.get(Unit, (c.id, "tank"))).amount = 100
        (await session.get(Building, (c.id, "lumber"))).amount = 100
        event = Disaster(day="2030-01-01", scheduled_at=utcnow(), kind=kind, damage=0.03)
        session.add(event)
        await session.flush()
        await apply_disaster(session, RULES, event, utcnow())
        await apply_disaster(session, RULES, event, utcnow())
    state = await snapshot(world)
    dependent = await snapshot(world, 102)
    assert dependent["resources"] == {key: RULES["initial"]["resources"].get(key, 0) for key in RULES["resources"]}
    assert dependent["population"] == RULES["initial"]["population"]
    assert len(state["events"]) == 1
    if kind == "virus":
        assert state["population"] == 194
    elif kind == "fire":
        assert state["resources"]["wood"] == 1940
    elif kind == "earthquake":
        assert state["buildings"]["house"] == 59 and state["population"] == 196
    elif kind == "aliens":
        assert state["units"]["tank"] == 97 and state["soldiers"] == 97 and state["spies"] == 97
    elif kind == "meteor":
        assert state["buildings"]["lumber"] == 97
        assert state["buildings"]["iron_mine"] == 2  # a small holding never loses 50% from rounding


async def test_restart_and_parallel_workers_pay_once(world):
    now = utcnow()
    await seed_jobs(world, RULES, now)
    async with world.sessions() as session, session.begin():
        await session.execute(text("UPDATE jobs SET next_run=:future"), {"future": now + timedelta(days=1)})
        job = await session.get(Job, "production")
        job.next_run = now
    await seed_jobs(world, RULES, now)  # Restart must preserve existing scheduler state.
    before = await snapshot(world)
    results = await asyncio.gather(run_once(world, RULES, now), run_once(world, RULES, now))
    assert sorted(results) == [False, True]
    after = await snapshot(world)
    assert after["resources"]["wood"] - before["resources"]["wood"] == 500
    async with world.sessions() as session:
        assert (await session.get(Job, "production")).runs == 1


async def test_downtime_catchup_is_bounded_and_new_countries_not_backpaid(world):
    now = utcnow()
    await seed_jobs(world, RULES, now)
    async with world.sessions() as session, session.begin():
        await session.execute(text("UPDATE countries SET created_at=:past"), {"past": now - timedelta(days=2)})
        await session.execute(text("UPDATE jobs SET next_run=:future"), {"future": now + timedelta(days=1)})
        job = await session.get(Job, "production")
        job.next_run = now - timedelta(days=1)
    before = await snapshot(world)
    await run_once(world, RULES, now)
    after = await snapshot(world)
    assert after["resources"]["wood"] - before["resources"]["wood"] == 500 * RULES["jobs"]["max_catchup_ticks"]
    async with world.sessions() as session:
        assert (await session.get(Job, "production")).next_run > now


async def test_failed_job_rolls_back_resources_and_schedule(world, monkeypatch):
    import game.worker as worker

    now = utcnow()
    await seed_jobs(world, RULES, now)
    async with world.sessions() as session, session.begin():
        await session.execute(text("UPDATE jobs SET next_run=:future"), {"future": now + timedelta(days=1)})
        (await session.get(Job, "production")).next_run = now
    before = await snapshot(world)
    original = worker.production_tick

    async def broken(session, rules, at):
        await original(session, rules, at)
        raise RuntimeError("Simulated process crash before commit")

    monkeypatch.setattr(worker, "production_tick", broken)
    with pytest.raises(RuntimeError):
        await run_once(world, RULES, now)
    assert (await snapshot(world))["resources"] == before["resources"]
    async with world.sessions() as session:
        assert (await session.get(Job, "production")).runs == 0
    monkeypatch.setattr(worker, "production_tick", original)
    assert await run_once(world, RULES, now)
    assert (await snapshot(world))["resources"]["wood"] == before["resources"]["wood"] + 500


async def test_country_created_after_tick_gets_no_retroactive_income(world):
    before = await snapshot(world)
    at = utcnow() - timedelta(hours=1)
    async with world.sessions() as session, session.begin():
        await production_tick(session, RULES, at)
        await income_tick(session, RULES, at)
    after = await snapshot(world)
    assert after["resources"] == before["resources"] and after["money"] == before["money"]


async def test_daily_disaster_schedule_persists_and_applies_once(world):
    from game.ticks import disaster_tick

    class FixedRandom:
        def randrange(self, stop):
            return 0

        def choice(self, options):
            return "fire"

        def uniform(self, low, high):
            return 0.03

    now = utcnow()
    async with world.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE countries SET independent=true, created_at=:past"), {"past": now - timedelta(days=1)}
        )
        await disaster_tick(session, RULES, now, FixedRandom())
    after = await snapshot(world)
    assert after["resources"]["wood"] == 1940
    async with world.sessions() as session, session.begin():
        await disaster_tick(session, RULES, now, FixedRandom())
        assert len((await session.scalars(select(Disaster))).all()) == 1
    assert (await snapshot(world))["resources"] == after["resources"]


async def test_broken_energy_does_not_burn_fuel(world):
    state = await snapshot(world)
    async with world.sessions() as session, session.begin():
        c = await session.get(Country, state["id"])
        c.independent = True
        c.energy_condition = 0
        (await session.get(Building, (c.id, "nuclear"))).amount = 10
        (await session.get(Inventory, (c.id, "uranium"))).amount = 100
        await session.flush()
        await income_tick(session, RULES, utcnow())
    result = await snapshot(world)
    assert result["money"] - state["money"] == state["tax_income"]
    assert result["resources"]["uranium"] == 100
