"""Shared transactional primitives, intentionally independent of Telegram."""

from datetime import datetime
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from game.models import Building, Country, Inventory, Unit, MOSCOW


class GameError(ValueError):
    """A safe, human-readable domain validation error."""


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=MOSCOW) if value.tzinfo is None else value.astimezone(MOSCOW)


def positive_quantity(value: int, rules: dict) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= rules["limits"]["quantity"]:
        raise GameError(f"Введите целое число от 1 до {rules['limits']['quantity']:,}.")
    return value


async def country(session: AsyncSession, telegram_id: int, lock: bool = True) -> Country:
    stmt = select(Country).where(Country.telegram_id == telegram_id)
    if lock:
        stmt = stmt.with_for_update(key_share=True).execution_options(populate_existing=True)
    result = await session.scalar(stmt)
    if result is None:
        raise GameError("Сначала создайте страну: /create CountryName")
    return result


async def inventory(session: AsyncSession, country_id: int) -> dict[str, int]:
    rows = await session.execute(select(Inventory.resource, Inventory.amount).where(Inventory.country_id == country_id))
    return dict(rows.all())


async def spend(session: AsyncSession, owner: Country, money: int = 0, resources: dict | None = None) -> None:
    resources = resources or {}
    if money < 0 or any(v < 0 for v in resources.values()):
        raise ValueError("Negative debit")
    if owner.money < money:
        raise GameError(f"Недостаточно денег. Нужно {money:,}, доступно {owner.money:,}.")
    rows = {
        row.resource: row
        for row in (
            await session.scalars(
                select(Inventory)
                .where(Inventory.country_id == owner.id, Inventory.resource.in_(list(resources)))
                .order_by(Inventory.resource)
                .with_for_update()
            )
        ).all()
    }
    for key, amount in resources.items():
        if key not in rows or rows[key].amount < amount:
            from game.config import RULES

            title = RULES["resources"].get(key, {}).get("name", key)
            raise GameError(f"Недостаточно ресурса «{title}»: нужно {amount:,}.")
    owner.money -= money
    for key, amount in resources.items():
        rows[key].amount -= amount


async def _add(session: AsyncSession, cls, keys: dict, amount: int) -> None:
    row = await session.get(cls, keys)
    if row is None:
        if amount < 0:
            raise GameError("Недостаточно ресурсов или техники.")
        session.add(cls(**keys, amount=amount))
    elif row.amount + amount < 0:
        raise GameError("Недостаточно ресурсов или техники.")
    else:
        row.amount += amount


async def add_resource(session: AsyncSession, country_id: int, resource: str, amount: int) -> None:
    await _add(session, Inventory, {"country_id": country_id, "resource": resource}, amount)


async def add_unit(session: AsyncSession, country_id: int, kind: str, amount: int) -> None:
    await _add(session, Unit, {"country_id": country_id, "kind": kind}, amount)


async def add_building(session: AsyncSession, country_id: int, kind: str, amount: int) -> None:
    await _add(session, Building, {"country_id": country_id, "kind": kind}, amount)


def power_expression(rules: dict):
    weight = case(*[(Unit.kind == key, value["power"]) for key, value in rules["units"].items()], else_=0)
    equipment = (
        select(func.coalesce(func.sum(Unit.amount * weight), 0))
        .where(Unit.country_id == Country.id)
        .correlate(Country)
        .scalar_subquery()
    )
    return Country.soldiers + Country.spies + equipment


def production_expression(rules: dict):
    value = case(
        *[
            (Building.kind == key, spec["output"] * rules["resources"][spec["resource"]]["price"])
            for key, spec in rules["buildings"].items()
            if spec.get("resource")
        ],
        else_=0,
    )
    return (
        select(func.coalesce(func.sum(Building.amount * value), 0))
        .where(Building.country_id == Country.id)
        .correlate(Country)
        .scalar_subquery()
    )


def gdp_expression(rules: dict):
    denominator = case(
        (Country.population < rules["economy"]["income_population_floor"], rules["economy"]["income_population_floor"]),
        else_=Country.population,
    )
    return production_expression(rules) * 1.0 / denominator
