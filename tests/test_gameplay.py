"""Domain integration tests use SQLite; PostgreSQL concurrency needs its own integration suite."""

import random
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from game.config import RULES
from game.core import GameError, country, inventory
from game.models import (
    Base,
    Expedition,
    Inventory,
    MarketOrder,
    utcnow,
)
from game.services.country import GameService
from game.services.intelligence import IntelligenceService
from game.services.market import MarketService
from game.services.space import SpaceService


@pytest_asyncio.fixture
async def game_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        yield session
    await engine.dispose()


async def create(session, tg=101, name="Arcadia"):
    await GameService(session, RULES).create(tg, name)
    return await country(session, tg)


async def rich(session, owner):
    owner.money = 1_000_000
    owner.independent = True
    for row in (await session.scalars(select(Inventory).where(Inventory.country_id == owner.id))).all():
        row.amount = 20_000


async def test_names_and_country_uniqueness(game_session):
    service = GameService(game_session, RULES)
    for name in ("Мир", "123", "New Land", "AA", "x" * 25):
        with pytest.raises(GameError):
            await service.create(101, name)
    await create(game_session)
    with pytest.raises(GameError):
        await service.create(102, "ARCADIA")
    with pytest.raises(GameError):
        await service.create(101, "Atlantis")
    snapshot = await service.snapshot(101)
    assert snapshot["population"] == RULES["initial"]["population"]
    assert snapshot["resources"]["wood"] == RULES["initial"]["resources"]["wood"]
    assert snapshot["housing_capacity"] == RULES["initial"]["buildings"]["house"] * RULES["economy"]["house_capacity"]
    assert snapshot["tax_income"] == snapshot["population"] * RULES["economy"]["tax_per_person"]
    assert snapshot["gdp"] > 0


async def test_recruitment_is_population_transfer_and_spies_share_cap(game_session):
    owner = await create(game_session)
    service = GameService(game_session, RULES)
    maximum = int(owner.population * RULES["recruitment"]["maximum_share"])
    await service.recruit(101, maximum)
    assert owner.population + owner.soldiers == RULES["initial"]["population"]
    await IntelligenceService(game_session, RULES).train_spies(101, 1)
    assert owner.soldiers + owner.spies == maximum
    with pytest.raises(GameError):
        await service.recruit(101, 1)
    for invalid in (-1, 0, True, 2.5, RULES["limits"]["quantity"] + 1):
        with pytest.raises(GameError):
            await service.feed(101, invalid)


async def test_building_land_and_independence_gates(game_session):
    owner = await create(game_session)
    service = GameService(game_session, RULES)
    with pytest.raises(GameError):
        await service.build(101, "solar", 1)
    with pytest.raises(GameError):
        await service.build(101, "house", 1000)
    with pytest.raises(GameError):
        await service.independence(101)
    await rich(game_session, owner)
    owner.independent = False
    owner.population = RULES["independence"]["population"]
    owner.soldiers = RULES["independence"]["military_power"]
    await service.independence(101)
    assert owner.independent
    before = owner.land
    await service.expand(101, 1)
    assert owner.land == before + RULES["expansion"]["land_per_purchase"]


async def test_market_escrow_conserves_money_and_resources(game_session):
    seller = await create(game_session)
    buyer = await create(game_session, 102, "Borealis")
    market = MarketService(game_session, RULES)
    original = (await inventory(game_session, seller.id))["wood"]
    total_money = seller.money + buyer.money
    await market.create_order(101, "wood", 100)
    order = await game_session.scalar(select(MarketOrder))
    assert (await inventory(game_session, seller.id))["wood"] == original - 100
    await market.buy_order(102, order.id, 40)
    assert seller.money + buyer.money == total_money
    assert order.amount == 60
    with pytest.raises(GameError):
        await market.buy_order(102, order.id, 100)
    with pytest.raises(GameError):
        await market.cancel_order(102, order.id)
    await market.cancel_order(101, order.id)
    assert (await inventory(game_session, seller.id))["wood"] == original - 40
    assert order.amount == 0
    with pytest.raises(GameError):
        await market.cancel_order(101, order.id)
    with pytest.raises(GameError):
        await market.create_order(101, "rocket", 1)


async def test_system_market_has_no_arbitrage(game_session):
    owner = await create(game_session)
    market = MarketService(game_session, RULES)
    before = owner.money
    await market.system_buy(101, "water", 100)
    await market.system_sell(101, "water", 100)
    assert owner.money < before


async def test_transfers_enforce_age_incoming_outgoing_and_fee(game_session):
    sender = await create(game_session)
    recipient = await create(game_session, 102, "Borealis")
    market = MarketService(game_session, RULES)
    with pytest.raises(GameError):
        await market.transfer(101, 102, 100)
    sender.created_at = recipient.created_at = utcnow() - timedelta(days=2)
    before = sender.money + recipient.money
    await market.transfer(101, 102, 100)
    assert sender.money + recipient.money < before
    assert recipient.money == RULES["initial"]["money"] + 100
    with pytest.raises(GameError):
        await market.transfer(101, 102, RULES["market"]["transfer_daily_absolute"])


async def test_space_is_lazy_exactly_once_and_returns_surviving_rocket(game_session):
    owner = await create(game_session)
    await rich(game_session, owner)
    rocket = await game_session.get(Inventory, (owner.id, "rocket"))
    rocket.amount = 1
    service = SpaceService(game_session, RULES, rng=random.Random(0))
    await service.launch(101)
    assert rocket.amount == 0
    expedition = await game_session.get(Expedition, owner.id)
    assert not expedition.destroyed
    with pytest.raises(GameError):
        await service.launch(101)
    with pytest.raises(GameError):
        await service.claim(101)
    expedition.returns_at = utcnow() - timedelta(seconds=1)
    await service.claim(101)
    await game_session.flush()
    assert rocket.amount == 1
    assert await game_session.get(Expedition, owner.id) is None
    with pytest.raises(GameError):
        await service.claim(101)
    with pytest.raises(GameError):
        await service.launch(101)


async def test_exploded_rocket_is_lost_without_loot(game_session):
    owner = await create(game_session)
    await rich(game_session, owner)
    rocket = await game_session.get(Inventory, (owner.id, "rocket"))
    rocket.amount = 1
    before = await inventory(game_session, owner.id)
    service = SpaceService(game_session, RULES, rng=random.Random(1))
    await service.launch(101)
    expedition = await game_session.get(Expedition, owner.id)
    assert expedition.destroyed
    expedition.returns_at = utcnow() - timedelta(seconds=1)
    await service.claim(101)
    after = await inventory(game_session, owner.id)
    assert rocket.amount == 0
    assert all(after[key] == before[key] for key in RULES["space"]["rewards"])


async def test_failed_spy_operation_consumes_cooldown(game_session):
    attacker = await create(game_session)
    target = await create(game_session, 102, "Borealis")
    attacker.spies = 10
    target.created_at = utcnow() - timedelta(days=2)
    service = IntelligenceService(game_session, RULES, rng=random.Random(0))
    result = await service.spy(101, "wood")
    assert "провалилась" in result
    assert attacker.spy_ready_at is not None
    assert attacker.spies < 10
    with pytest.raises(GameError):
        await service.spy(101, "wood")


async def test_successful_spy_theft_conserves_resources(game_session):
    attacker = await create(game_session)
    target = await create(game_session, 102, "Borealis")
    attacker.spies = 10
    target.created_at = utcnow() - timedelta(days=2)
    before_attacker = (await inventory(game_session, attacker.id))["wood"]
    before_target = (await inventory(game_session, target.id))["wood"]
    result = await IntelligenceService(game_session, RULES, rng=random.Random(1)).spy(101, "wood")
    assert "доставила" in result
    after_attacker = (await inventory(game_session, attacker.id))["wood"]
    after_target = (await inventory(game_session, target.id))["wood"]
    assert after_attacker > before_attacker
    assert after_attacker + after_target == before_attacker + before_target


async def test_hacking_unlock_and_cooldown(game_session):
    attacker = await create(game_session)
    target = await create(game_session, 102, "Borealis")
    service = IntelligenceService(game_session, RULES, rng=random.Random(1))
    target.created_at = utcnow() - timedelta(days=2)
    with pytest.raises(GameError):
        await service.research_hackers(101)
    await rich(game_session, attacker)
    await service.research_hackers(101)
    report = await service.hack(101)
    assert "Баланс" in report and "Borealis" in report
    with pytest.raises(GameError):
        await service.hack(101)


async def test_energy_maintenance_scales_with_station_count(game_session):
    from game.models import Building

    owner = await create(game_session)
    await rich(game_session, owner)
    owner.energy_condition = 10
    service = GameService(game_session, RULES)
    with pytest.raises(GameError):
        await service.energy_maintain(101)
    solar = await game_session.get(Building, (owner.id, "solar"))
    solar.amount = 2
    nuclear = await game_session.get(Building, (owner.id, "nuclear"))
    nuclear.amount = 1
    before = owner.money
    weight = (
        2 * RULES["energy"]["maintenance_station_weights"]["solar"]
        + RULES["energy"]["maintenance_station_weights"]["nuclear"]
    )
    await service.energy_maintain(101)
    assert before - owner.money == RULES["energy"]["maintenance_money"] * weight
    assert owner.energy_condition == 10 + RULES["energy"]["maintenance_restore"]


async def test_space_artifact_crafting_and_system_redemption(game_session):
    owner = await create(game_session)
    await rich(game_session, owner)
    service = SpaceService(game_session, RULES)
    artifact = await game_session.get(Inventory, (owner.id, "quantum_core"))
    artifact.amount = 0
    before = owner.money
    await service.craft_item(101, "quantum_core", 1)
    assert artifact.amount == 1
    await service.sell_item(101, "quantum_core", 1)
    assert artifact.amount == 0
    cfg = RULES["space"]["items"]["quantum_core"]
    assert owner.money == before - cfg["money"] + cfg["sell_price"]
    with pytest.raises(GameError):
        await service.sell_item(101, "quantum_core", 1)
