"""Espionage and intelligence actions; outcomes and cooldowns commit together."""

from __future__ import annotations

import math
import random
from game.formatting import number, remaining, money

from datetime import timedelta
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from game.core import (
    GameError,
    add_resource,
    aware,
    country,
    gdp_expression,
    inventory,
    positive_quantity,
    power_expression,
    spend,
)
from game.models import AllianceMember, Country, GameEvent, utcnow
from game.services.country import require_independent, scaled_cost


class IntelligenceService:
    def __init__(self, session: AsyncSession, rules: dict[str, Any], rng: random.Random | None = None):
        self.session, self.rules = session, rules
        self.rng = rng or random.SystemRandom()

    async def train_spies(self, telegram_id: int, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        state = await country(self.session, telegram_id)
        cfg = self.rules["intelligence"]
        if state.soldiers < quantity:
            raise GameError("Для обучения нужны действующие военнослужащие.")
        await spend(
            self.session,
            state,
            money=quantity * cfg["spy_training_money"],
            resources={"food": quantity * cfg["spy_training_food"]},
        )
        state.soldiers -= quantity
        state.spies += quantity
        return f"Обучено {number(quantity)} разведчиков. Они входят в лимит армии и едят из армейского запаса."

    async def research_spies(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        cfg = self.rules["intelligence"]
        if state.spy_level >= cfg["max_level"]:
            raise GameError("Разведка уже достигла максимального уровня.")
        await spend(
            self.session,
            state,
            money=cfg["spy_research_money"] * state.spy_level,
            resources=scaled_cost(cfg["spy_research_cost"], state.spy_level),
        )
        state.spy_level += 1
        return f"Уровень разведки повышен до {state.spy_level}."

    async def research_hackers(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        require_independent(state)
        cfg = self.rules["intelligence"]
        if state.hacker_level >= cfg["max_level"]:
            raise GameError("Киберподразделение уже достигло максимального уровня.")
        level_cost = state.hacker_level + 1
        await spend(
            self.session,
            state,
            money=cfg["hack_research_money"] * level_cost,
            resources=scaled_cost(cfg["hack_research_cost"], level_cost),
        )
        state.hacker_level += 1
        return f"Киберподразделение: уровень {state.hacker_level}."

    async def _targets(self, telegram_id: int, target_id: int) -> tuple[Country, Country]:
        if telegram_id == target_id:
            raise GameError("Выберите другую страну.")
        states = (
            await self.session.scalars(
                select(Country)
                .where(Country.telegram_id.in_([telegram_id, target_id]))
                .order_by(Country.id)
                .with_for_update(key_share=True)
            )
        ).all()
        by_tg = {state.telegram_id: state for state in states}
        if telegram_id not in by_tg or target_id not in by_tg:
            raise GameError("Страна не найдена. Нужен Telegram ID президента.")
        attacker, target = by_tg[telegram_id], by_tg[target_id]
        cfg = self.rules["intelligence"]
        if aware(target.created_at) > utcnow() - timedelta(hours=cfg["new_player_protection_hours"]):
            raise GameError("Эта страна пока находится под защитой новичка.")
        memberships = (
            await self.session.scalars(
                select(AllianceMember).where(AllianceMember.country_id.in_([attacker.id, target.id]))
            )
        ).all()
        if len(memberships) == 2 and memberships[0].alliance_id == memberships[1].alliance_id:
            raise GameError("Нельзя атаковать участника своего союза.")
        return attacker, target

    async def random_target(self, telegram_id):
        actor = await country(self.session, telegram_id, lock=False)
        gdp = await self.session.scalar(select(gdp_expression(self.rules)).where(Country.id == actor.id))
        gdp = float(gdp)
        cfg = self.rules["intelligence"]
        own = await self.session.scalar(select(AllianceMember.alliance_id).where(AllianceMember.country_id == actor.id))
        allies = (
            select(AllianceMember.country_id).where(AllianceMember.alliance_id == own)
            if own
            else select(Country.id).where(Country.id == actor.id)
        )
        target = await self.session.scalar(
            select(Country.telegram_id)
            .where(
                Country.id != actor.id,
                Country.id.not_in(allies),
                Country.created_at <= utcnow() - timedelta(hours=cfg["new_player_protection_hours"]),
                gdp_expression(self.rules).between(gdp * (1 - cfg["gdp_tolerance"]), gdp * (1 + cfg["gdp_tolerance"])),
            )
            .order_by(func.random())
            .limit(1)
        )
        if target is None:
            raise GameError("Нет подходящей цели по ВВП. Повторите позже.")
        return target

    async def spy(self, telegram_id: int, resource: str) -> str:
        return await self._spy_target(telegram_id, await self.random_target(telegram_id), resource)

    async def hack(self, telegram_id: int) -> str:
        return await self._hack_target(telegram_id, await self.random_target(telegram_id))

    async def _spy_target(self, telegram_id: int, target_id: int, resource: str) -> str:
        resource_cfg = self.rules["resources"].get(resource)
        if not resource_cfg or not resource_cfg["tradable"]:
            raise GameError("Разведчики могут похищать только обычные торговые ресурсы.")
        attacker, target = await self._targets(telegram_id, target_id)
        cfg = self.rules["intelligence"]
        now = utcnow()
        if not attacker.spies:
            raise GameError("Сначала обучите разведчиков.")
        if attacker.spy_ready_at and aware(attacker.spy_ready_at) > now:
            raise GameError(f"Следующая разведоперация: {remaining(aware(attacker.spy_ready_at))}.")
        stock = await inventory(self.session, target.id)
        amount = min(
            int(stock.get(resource, 0) * cfg["spy_theft_share"]), attacker.spies * cfg["spy_capacity_per_agent"]
        )
        if amount <= 0:
            raise GameError("На складе цели слишком мало выбранного ресурса.")
        chance = min(
            cfg["spy_max_chance"],
            max(
                cfg["spy_min_chance"],
                cfg["spy_base_chance"]
                + attacker.spy_level * cfg["spy_level_chance"]
                + min(cfg["spy_agent_bonus_cap"], attacker.spies * cfg["spy_per_agent_chance"])
                - target.spy_level * cfg["spy_defense_per_level"],
            ),
        )
        attacker.spy_ready_at = now + timedelta(hours=cfg["spy_cooldown_hours"])
        if self.rng.random() >= chance:
            losses = min(attacker.spies, math.ceil(attacker.spies * cfg["spy_death_share"]))
            attacker.spies -= losses
            self.session.add(
                GameEvent(country_id=target.id, text=f"Контрразведка пресекла операцию страны {attacker.name}.")
            )
            return f"Операция провалилась (шанс успеха {chance:.0%}). Потеряно разведчиков: {number(losses)}."
        await spend(self.session, target, resources={resource: amount})
        await add_resource(self.session, attacker.id, resource, amount)
        self.session.add(
            GameEvent(
                country_id=target.id, text=f"На складе обнаружена пропажа: {resource_cfg['name']} × {number(amount)}."
            )
        )
        return f"Разведка доставила {resource_cfg['name']} × {number(amount)} из страны {target.name}. Шанс операции был {chance:.0%}."

    async def _hack_target(self, telegram_id: int, target_id: int) -> str:
        attacker, target = await self._targets(telegram_id, target_id)
        require_independent(attacker)
        cfg = self.rules["intelligence"]
        now = utcnow()
        if attacker.hacker_level <= 0:
            raise GameError("Сначала откройте киберподразделение.")
        if attacker.hack_ready_at and aware(attacker.hack_ready_at) > now:
            raise GameError(f"Следующая кибероперация: {remaining(aware(attacker.hack_ready_at))}.")
        chance = min(cfg["hack_max_chance"], cfg["hack_base_chance"] + attacker.hacker_level * cfg["hack_level_chance"])
        attacker.hack_ready_at = now + timedelta(hours=cfg["hack_cooldown_hours"])
        if self.rng.random() >= chance:
            return f"Кибероперация не удалась. Шанс успеха: {chance:.0%}. Улучшайте подразделение."
        power, gdp = (
            await self.session.execute(
                select(power_expression(self.rules), gdp_expression(self.rules)).where(Country.id == target.id)
            )
        ).one()
        return (
            f"Разведсводка: {target.name}\nБаланс: {money(target.money)}\nНаселение: {number(target.population)}\n"
            f"ВВП на жителя за цикл: {number(float(gdp))}\nВоенная мощь: {int(power)}\n"
            f"Военнослужащие: {number(target.soldiers)}\nРазведчики: {number(target.spies)}\n"
            f"Доход с налогов: {target.population * self.rules['economy']['tax_per_person']}/час\n"
            f"Шанс операции: {chance:.0%}."
        )
