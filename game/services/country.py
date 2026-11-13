"""Player-driven country actions. The caller owns the database transaction."""

from __future__ import annotations

import re
from game.formatting import number

from typing import Any

from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from game.core import (
    GameError,
    add_building,
    add_unit,
    country,
    gdp_expression,
    inventory,
    positive_quantity,
    power_expression,
    spend,
)
from game.models import Building, Country, Expedition, GameEvent, Inventory, Unit, CountryWar


def scaled_cost(cost: dict[str, int], quantity: int) -> dict[str, int]:
    return {key: amount * quantity for key, amount in cost.items()}


def require_independent(state: Country) -> None:
    if not state.independent:
        raise GameError("Сначала получите независимость в разделе «Развитие».")


class GameService:
    def __init__(self, session: AsyncSession, rules: dict[str, Any]):
        self.session = session
        self.rules = rules

    async def create(self, telegram_id: int, name: str) -> str:
        cfg = self.rules["country"]
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z]+", name) or not cfg["name_min"] <= len(name) <= cfg["name_max"]:
            raise GameError(f"Название: от {cfg['name_min']} до {cfg['name_max']} латинских букв, без пробелов и цифр.")
        existing = await self.session.scalar(select(Country.id).where(Country.telegram_id == telegram_id))
        if existing:
            raise GameError("У вас уже есть страна. Откройте игровую панель.")
        if await self.session.scalar(select(Country.id).where(Country.name_key == name.lower())):
            raise GameError("Такое название уже занято (регистр букв не учитывается).")
        initial = self.rules["initial"]
        state = Country(
            telegram_id=telegram_id,
            name=name,
            name_key=name.lower(),
            money=initial["money"],
            population=initial["population"],
            land=initial["land"],
            hunger=cfg["initial_hunger"],
            army_hunger=cfg["initial_army_hunger"],
            spy_level=cfg["initial_spy_level"],
            hacker_level=cfg["initial_hacker_level"],
            energy_level=cfg["initial_energy_level"],
            energy_condition=cfg["initial_energy_condition"],
        )
        # SAVEPOINT keeps a duplicate registration from poisoning the outer transaction.
        try:
            async with self.session.begin_nested():
                self.session.add(state)
                await self.session.flush()
        except IntegrityError as exc:
            raise GameError("Страна уже создана или это название только что заняли.") from exc
        for resource in self.rules["resources"]:
            self.session.add(
                Inventory(country_id=state.id, resource=resource, amount=initial["resources"].get(resource, 0))
            )
        for kind in self.rules["buildings"]:
            self.session.add(Building(country_id=state.id, kind=kind, amount=initial["buildings"].get(kind, 0)))
        for kind in self.rules["units"]:
            self.session.add(Unit(country_id=state.id, kind=kind, amount=0))
        await self.session.flush()
        return f"Страна {name} основана! Развивайте производство, обеспечивайте людей едой и получите независимость."

    async def snapshot(self, telegram_id: int) -> dict[str, Any]:
        state = await country(self.session, telegram_id, lock=False)
        buildings = dict(
            (
                await self.session.execute(
                    select(Building.kind, Building.amount).where(Building.country_id == state.id)
                )
            ).all()
        )
        units = dict(
            (await self.session.execute(select(Unit.kind, Unit.amount).where(Unit.country_id == state.id))).all()
        )
        values = (
            await self.session.execute(
                select(power_expression(self.rules), gdp_expression(self.rules)).where(Country.id == state.id)
            )
        ).one()
        used = sum(self.rules["buildings"][kind]["land"] * qty for kind, qty in buildings.items())
        events = list(
            (
                await self.session.scalars(
                    select(GameEvent)
                    .where(GameEvent.country_id == state.id)
                    .order_by(GameEvent.id.desc())
                    .limit(self.rules["country"]["ranking_limit"])
                )
            ).all()
        )
        expedition = await self.session.get(Expedition, state.id)
        return {
            "id": state.id,
            "telegram_id": state.telegram_id,
            "name": state.name,
            "money": state.money,
            "population": state.population,
            "land": state.land,
            "used_land": used,
            "free_land": state.land - used,
            "hunger": state.hunger,
            "army_hunger": state.army_hunger,
            "army_food": state.army_food,
            "civilian_food": state.civilian_food,
            "soldiers": state.soldiers,
            "spies": state.spies,
            "spy_level": state.spy_level,
            "hacker_level": state.hacker_level,
            "independent": state.independent,
            "military_power": int(values[0]),
            "gdp": float(values[1]),
            "tax_income": state.population * self.rules["economy"]["tax_per_person"],
            "energy_level": state.energy_level,
            "energy_condition": state.energy_condition,
            "resources": await inventory(self.session, state.id),
            "buildings": buildings,
            "units": units,
            "housing_capacity": buildings.get("house", 0) * self.rules["economy"]["house_capacity"],
            "space_ready_at": state.space_ready_at,
            "spy_ready_at": state.spy_ready_at,
            "hack_ready_at": state.hack_ready_at,
            "expedition_returns_at": expedition.returns_at if expedition else None,
            "events": [{"text": event.text, "created_at": event.created_at} for event in events],
        }

    async def rankings(self, metric: str = "money") -> list[dict[str, Any]]:
        power = power_expression(self.rules)
        gdp = gdp_expression(self.rules)
        wars = (
            select(func.count(CountryWar.id))
            .where(
                CountryWar.resolved_at.is_not(None),
                or_(CountryWar.attacker_id == Country.id, CountryWar.defender_id == Country.id),
            )
            .correlate(Country)
            .scalar_subquery()
        )
        metrics = {"wars": wars, "money": Country.money, "power": power, "military_power": power, "gdp": gdp}
        if metric not in metrics:
            raise GameError("Неизвестный рейтинг.")
        stmt = (
            select(
                Country.id,
                Country.telegram_id,
                Country.name,
                Country.money,
                power.label("military_power"),
                gdp.label("gdp"),
                wars.label("wars"),
            )
            .order_by(metrics[metric].desc(), Country.id)
            .limit(self.rules["country"]["ranking_limit"])
        )
        return [dict(row) for row in (await self.session.execute(stmt)).mappings()]

    async def build(self, telegram_id: int, kind: str, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        cfg = self.rules["buildings"].get(kind)
        if not cfg:
            raise GameError("Неизвестное предприятие.")
        state = await country(self.session, telegram_id)
        if cfg.get("independent"):
            require_independent(state)
        buildings = (
            await self.session.execute(select(Building.kind, Building.amount).where(Building.country_id == state.id))
        ).all()
        used = sum(self.rules["buildings"][key]["land"] * amount for key, amount in buildings)
        if used + cfg["land"] * quantity > state.land:
            raise GameError("Не хватает территории. Сначала расширьте страну.")
        await spend(self.session, state, money=cfg["money"] * quantity, resources=scaled_cost(cfg["cost"], quantity))
        await add_building(self.session, state.id, kind, quantity)
        return f"Построено: {cfg['name']} × {number(quantity)}."

    async def expand(self, telegram_id: int, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        state = await country(self.session, telegram_id)
        cfg = self.rules["expansion"]
        # Every additional parcel gets more expensive; buying a batch cannot bypass progression.
        scale = sum(1 + (state.land + i * cfg["land_per_purchase"]) // cfg["scaling_land"] for i in range(quantity))
        await spend(
            self.session,
            state,
            money=cfg["money"] * scale,
            resources={"wood": cfg["wood"] * scale, "stone": cfg["stone"] * scale},
        )
        acquired = cfg["land_per_purchase"] * quantity
        state.land += acquired
        return f"Территория расширена на {number(acquired)}. Всего: {number(state.land)}."

    async def recruit(self, telegram_id: int, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        state = await country(self.session, telegram_id)
        cfg = self.rules["recruitment"]
        total_people = state.population + state.soldiers + state.spies
        allowed = int(total_people * cfg["maximum_share"]) - state.soldiers - state.spies
        if quantity > allowed or state.population - quantity < cfg["minimum_civilians"]:
            raise GameError(
                f"Сейчас можно призвать не более {max(0, min(allowed, state.population - cfg['minimum_civilians']))} человек. Лимит армии: {cfg['maximum_share']:.0%} граждан."
            )
        await spend(
            self.session,
            state,
            money=quantity * cfg["money_per_soldier"],
            resources={"food": quantity * cfg["food_per_soldier"]},
        )
        state.population -= quantity
        state.soldiers += quantity
        return f"Обучено {number(quantity)} военнослужащих. Не забывайте выделять армии продовольствие."

    async def craft(self, telegram_id: int, kind: str, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        cfg = self.rules["units"].get(kind)
        if not cfg:
            raise GameError("Неизвестный вид техники.")
        state = await country(self.session, telegram_id)
        await spend(self.session, state, money=cfg["money"] * quantity, resources=scaled_cost(cfg["cost"], quantity))
        await add_unit(self.session, state.id, kind, quantity)
        return f"Произведено: {cfg['name']} × {number(quantity)} (+{cfg['power'] * quantity} мощи)."

    async def feed(self, telegram_id: int, quantity: int) -> str:
        return await self.allocate_food(telegram_id, quantity, army=True)

    async def feed_population(self, telegram_id: int, quantity: int) -> str:
        return await self.allocate_food(telegram_id, quantity, army=False)

    async def allocate_food(self, telegram_id: int, quantity: int, army: bool) -> str:
        import math

        positive_quantity(quantity, self.rules)
        state = await country(self.session, telegram_id)
        people = state.soldiers + state.spies if army else state.population
        if not people:
            raise GameError("Некому выдавать продовольствие.")
        cfg = self.rules["economy"]
        per_percent = max(1, math.ceil(people * cfg["food_per_percent_army" if army else "food_per_percent_civilian"]))
        field = "army_hunger" if army else "hunger"
        restore = min(100 - getattr(state, field), quantity / per_percent)
        used = min(quantity, math.ceil(restore * per_percent))
        await spend(self.session, state, resources={"food": quantity})
        setattr(state, field, min(100, getattr(state, field) + restore))
        reserve = "army_food" if army else "civilian_food"
        setattr(state, reserve, getattr(state, reserve) + quantity - used)
        return f"Выдано {number(quantity)} ед. еды. Сытость +{restore:.1f}%. Остаток направлен в резерв."

    async def independence(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        if state.independent:
            raise GameError("Ваша страна уже независима.")
        cfg = self.rules["independence"]
        power, gdp = (
            await self.session.execute(
                select(power_expression(self.rules), gdp_expression(self.rules)).where(Country.id == state.id)
            )
        ).one()
        if state.population < cfg["population"] or power < cfg["military_power"] or gdp < cfg["gdp"]:
            raise GameError(
                f"Для независимости нужны: население {cfg['population']}, военная мощь {cfg['military_power']}, ВВП на жителя {cfg['gdp']}. Сейчас: {state.population}, {int(power)}, {float(gdp):.2f}."
            )
        await spend(self.session, state, money=cfg["money"])
        state.independent = True
        return "Независимость признана! Открыты союзы, космос и энергетика. Теперь страна также подвержена общим катаклизмам."

    async def energy_upgrade(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        require_independent(state)
        cfg = self.rules["energy"]
        if state.energy_level >= cfg["max_level"]:
            raise GameError("Достигнут максимальный уровень энергетики.")
        await spend(
            self.session,
            state,
            money=cfg["upgrade_money"] * state.energy_level,
            resources=scaled_cost(cfg["upgrade_cost"], state.energy_level),
        )
        state.energy_level += 1
        return f"Энергетика улучшена до уровня {state.energy_level}."

    async def energy_maintain(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        require_independent(state)
        cfg = self.rules["energy"]
        if state.energy_condition >= self.rules["energy"]["condition_max"]:
            raise GameError("Электростанции уже в идеальном состоянии.")
        plants = (
            await self.session.execute(select(Building.kind, Building.amount).where(Building.country_id == state.id))
        ).all()
        weight = sum(cfg["maintenance_station_weights"].get(kind, 0) * amount for kind, amount in plants)
        if not weight:
            raise GameError("Сначала постройте электростанцию.")
        await spend(
            self.session,
            state,
            money=cfg["maintenance_money"] * weight,
            resources=scaled_cost(cfg["maintenance_cost"], weight),
        )
        state.energy_condition = min(
            self.rules["energy"]["condition_max"], state.energy_condition + cfg["maintenance_restore"]
        )
        return f"Обслуживание завершено. Состояние станций: {state.energy_condition:.0f}%."
