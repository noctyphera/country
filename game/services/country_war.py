"""Persistent bilateral country wars, resolved once by the world worker."""

from game.formatting import number
from game.notifications import enqueue

import random
import math
from datetime import timedelta
from sqlalchemy import select, exists, or_, func
from game.core import GameError, country, power_expression, aware
from game.models import Country, CountryWar, Inventory, Unit, GameEvent, AllianceMember, utcnow


class CountryWarService:
    def __init__(self, session, rules, rng=None):
        self.session, self.rules = session, rules
        self.rng = rng or random.SystemRandom()

    async def start(self, tg):
        actor = await country(self.session, tg, lock=False)
        now = utcnow()
        cfg = self.rules["country_war"]
        power = await self.session.scalar(select(power_expression(self.rules)).where(Country.id == actor.id))
        power = int(power)
        if power < cfg["minimum_power"]:
            raise GameError(f"Для войны нужна мощь {cfg['minimum_power']}.")
        active = exists(
            select(CountryWar.id).where(
                CountryWar.resolved_at.is_(None),
                or_(CountryWar.attacker_id == Country.id, CountryWar.defender_id == Country.id),
            )
        )
        own_alliance = await self.session.scalar(
            select(AllianceMember.alliance_id).where(AllianceMember.country_id == actor.id)
        )
        ally = (
            exists(
                select(AllianceMember.country_id).where(
                    AllianceMember.country_id == Country.id, AllianceMember.alliance_id == own_alliance
                )
            )
            if own_alliance
            else False
        )
        candidate = await self.session.scalar(
            select(Country.id)
            .where(
                Country.id != actor.id,
                ~active,
                ~ally if own_alliance else True,
                or_(Country.shield_until.is_(None), Country.shield_until <= now),
                or_(Country.war_ready_at.is_(None), Country.war_ready_at <= now),
                power_expression(self.rules).between(
                    power * (1 - cfg["power_tolerance"]), power * (1 + cfg["power_tolerance"])
                ),
            )
            .order_by(func.random())
            .limit(1)
        )
        if not candidate:
            raise GameError("Подходящий противник пока не найден.")
        states = {
            c.id: c
            for c in await self.session.scalars(
                select(Country)
                .where(Country.id.in_(sorted([actor.id, candidate])))
                .order_by(Country.id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        }
        for c in states.values():
            busy = await self.session.scalar(
                select(CountryWar.id).where(
                    CountryWar.resolved_at.is_(None),
                    or_(CountryWar.attacker_id == c.id, CountryWar.defender_id == c.id),
                )
            )
            if (
                busy
                or (c.shield_until and aware(c.shield_until) > now)
                or (c.war_ready_at and aware(c.war_ready_at) > now)
            ):
                raise GameError("Страна сейчас недоступна для войны. Повторите поиск позже.")
        values = {
            cid: await self.session.scalar(select(power_expression(self.rules)).where(Country.id == cid))
            for cid in states
        }
        values = {cid: int(value) for cid, value in values.items()}
        if (
            not values[actor.id] * (1 - cfg["power_tolerance"])
            <= values[candidate]
            <= values[actor.id] * (1 + cfg["power_tolerance"])
        ):
            raise GameError("Мощь соперника изменилась. Повторите поиск.")
        war = CountryWar(
            attacker_id=actor.id,
            defender_id=candidate,
            attacker_power=values[actor.id],
            defender_power=values[candidate],
            ends_at=now + timedelta(hours=cfg["duration_hours"]),
        )
        self.session.add(war)
        for c in states.values():
            self.session.add(
                GameEvent(
                    country_id=c.id,
                    text=f"Началась война: {states[actor.id].name} и {states[candidate].name}. Срок {cfg['duration_hours']} ч.",
                )
            )
        enqueue(
            self.session,
            states[candidate].telegram_id,
            f"На вашу страну напала {states[actor.id].name}. Военная мощь нападающего: {number(values[actor.id])}. Ваша мощь: {number(values[candidate])}.",
        )
        return f"Противник: {states[candidate].name}. Мощь {values[actor.id]} / {values[candidate]}. Бой продлится {cfg['duration_hours']} ч."

    async def resolve(self, now=None):
        now = now or utcnow()
        cfg = self.rules["country_war"]
        due = list(
            await self.session.scalars(
                select(CountryWar)
                .where(CountryWar.resolved_at.is_(None), CountryWar.ends_at <= now)
                .order_by(CountryWar.id)
                .limit(cfg["batch_size"])
            )
        )
        ids = sorted({cid for w in due for cid in (w.attacker_id, w.defender_id)})
        if not ids:
            return 0
        states = {
            c.id: c
            for c in await self.session.scalars(
                select(Country)
                .where(Country.id.in_(ids))
                .order_by(Country.id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        }
        count = 0
        for pending in due:
            w = await self.session.scalar(
                select(CountryWar)
                .where(CountryWar.id == pending.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if w.resolved_at:
                continue
            chance = 0.5 + cfg["strength_advantage"] * (w.attacker_power - w.defender_power) / max(
                1, w.attacker_power + w.defender_power
            )
            winner = states[w.attacker_id if self.rng.random() < chance else w.defender_id]
            loser = states[w.defender_id if winner.id == w.attacker_id else w.attacker_id]
            loot = math.floor(loser.money * cfg["loot_share"])
            loser.money -= loot
            winner.money += loot
            stocks = list(
                await self.session.scalars(
                    select(Inventory)
                    .where(Inventory.country_id.in_([winner.id, loser.id]))
                    .order_by(Inventory.country_id, Inventory.resource)
                )
            )
            bykey = {(r.country_id, r.resource): r for r in stocks}
            for r in stocks:
                if r.country_id == loser.id and self.rules["resources"].get(r.resource, {}).get("tradable"):
                    amount = math.floor(r.amount * cfg["loot_share"])
                    r.amount -= amount
                    target = bykey.get((winner.id, r.resource))
                    if target:
                        target.amount += amount
                    else:
                        self.session.add(Inventory(country_id=winner.id, resource=r.resource, amount=amount))
            for c in (winner, loser):
                for field in ("soldiers", "spies"):
                    amount = getattr(c, field)
                    setattr(
                        c,
                        field,
                        amount
                        - min(
                            amount,
                            math.floor(amount * cfg["loss_share"])
                            + int(self.rng.random() < (amount * cfg["loss_share"]) % 1),
                        ),
                    )
            for unit in await self.session.scalars(select(Unit).where(Unit.country_id.in_([winner.id, loser.id]))):
                exact = unit.amount * cfg["loss_share"]
                unit.amount -= min(unit.amount, math.floor(exact) + int(self.rng.random() < exact % 1))
            winner.war_ready_at = now + timedelta(hours=cfg["cooldown_hours"])
            loser.shield_until = now + timedelta(hours=cfg["shield_hours"])
            w.resolved_at = now
            w.winner_id = winner.id
            w.report = f"Победа: {winner.name}. Денежная добыча ¤{number(loot)}; ресурсы переданы на склад. Потери обеих сторон около {cfg['loss_share']:.0%}."
            for c in (winner, loser):
                self.session.add(GameEvent(country_id=c.id, text=w.report))
            count += 1
        return count


async def ensure_equipment_available(session, country_id):
    busy = await session.scalar(
        select(CountryWar.id)
        .where(
            CountryWar.resolved_at.is_(None),
            or_(CountryWar.attacker_id == country_id, CountryWar.defender_id == country_id),
        )
        .limit(1)
    )
    if busy:
        raise GameError("Во время войны страны нельзя продавать или передавать её технику.")
