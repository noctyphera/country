"""Transactional alliances, attributable equipment, and persistent timed wars.

The caller owns the transaction. Countries are locked before alliances, and
multiple alliances are always locked in primary-key order. No process-local
locks or scheduled tasks are needed to preserve war state after a restart.
"""

from __future__ import annotations

import math
import random
import re
from game.formatting import number, remaining, money

from datetime import datetime, timedelta

from sqlalchemy import case, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from game.core import GameError, aware, country
from game.notifications import enqueue
from game.models import (
    Alliance,
    AllianceInvitation,
    AllianceInventory,
    AllianceMember,
    AllianceUnit,
    Country,
    GameEvent,
    Unit,
    War,
    utcnow,
)


async def ensure_alliance_peace(session: AsyncSession, alliance_id: int) -> None:
    """Call while holding the alliance row lock before changing its assets."""
    active = await session.scalar(
        select(War.id)
        .where(
            War.status == "active",
            or_(War.attacker_id == alliance_id, War.defender_id == alliance_id),
        )
        .limit(1)
    )
    if active is not None:
        raise GameError("До завершения войны состав, техника и общак союза заморожены.")


class AllianceService:
    def __init__(self, session: AsyncSession, rules: dict):
        self.session = session
        self.rules = rules
        self.cfg = rules["alliance"]
        self.rng = random.SystemRandom()

    def _quantity(self, quantity: int) -> int:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 < quantity <= self.cfg["max_quantity"]:
            raise GameError("Укажите допустимое положительное целое количество.")
        return quantity

    def _unit(self, kind: str) -> dict:
        unit = self.rules["units"].get(kind)
        if unit is None or unit["power"] <= 0:
            raise GameError("Неизвестный вид военной техники.")
        return unit

    def _power_expression(self):
        return AllianceUnit.amount * case(
            {k: v["power"] for k, v in self.rules["units"].items()},
            value=AllianceUnit.kind,
            else_=0,
        )

    async def _lock_alliances(self, ids: list[int]) -> dict[int, Alliance]:
        rows = await self.session.scalars(
            select(Alliance).where(Alliance.id.in_(sorted(set(ids)))).order_by(Alliance.id).with_for_update()
        )
        return {row.id: row for row in rows}

    async def _membership(self, country_id: int) -> AllianceMember:
        member = await self.session.get(AllianceMember, country_id)
        if member is None:
            raise GameError("Ваша страна пока не состоит в союзе.")
        return member

    async def _context(self, tg: int, *, leader: bool = False, peaceful: bool = True):
        nation = await country(self.session, tg, lock=True)
        member = await self._membership(nation.id)
        alliance = (await self._lock_alliances([member.alliance_id]))[member.alliance_id]
        if leader and alliance.leader_country_id != nation.id:
            raise GameError("Это действие доступно только главе союза.")
        if peaceful:
            await ensure_alliance_peace(self.session, alliance.id)
        return nation, member, alliance

    async def _add_unit(self, aid: int, contributor: int, kind: str, quantity: int):
        if not quantity:
            return
        row = await self.session.get(AllianceUnit, (aid, contributor, kind))
        if row is None:
            self.session.add(
                AllianceUnit(alliance_id=aid, contributor_country_id=contributor, kind=kind, amount=quantity)
            )
        else:
            row.amount += quantity

    def _unit_value(self, kind):
        return self.rules["market"]["unit_prices"][kind]

    async def _equipment(self, aid: int) -> tuple[int, int]:
        rows = (await self.session.scalars(select(AllianceUnit).where(AllianceUnit.alliance_id == aid))).all()
        power = sum(row.amount * self._unit(row.kind)["power"] for row in rows)
        value = sum(row.amount * self._unit_value(row.kind) for row in rows)
        return power, value

    async def create(self, tg: int, name: str) -> str:
        nation = await country(self.session, tg, lock=True)
        if not nation.independent:
            raise GameError("Для создания союза нужна независимость.")
        if await self.session.get(AllianceMember, nation.id):
            raise GameError("Сначала покиньте текущий союз.")
        if (
            not (self.cfg["name_min_length"] <= len(name) <= self.cfg["name_max_length"])
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is None
        ):
            raise GameError(
                f"Название союза: {self.cfg['name_min_length']}–{self.cfg['name_max_length']} латинских букв, цифр или _, первая — буква."
            )
        if await self.session.scalar(select(Alliance.id).where(Alliance.name_key == name.lower())):
            raise GameError("Союз с таким названием уже существует.")
        if nation.money < self.cfg["creation_money"]:
            raise GameError("Недостаточно денег на создание союза.")
        nation.money -= self.cfg["creation_money"]
        alliance = Alliance(name=name, name_key=name.lower(), leader_country_id=nation.id, money=0)
        self.session.add(alliance)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise GameError("Союз с таким названием уже существует.") from exc
        self.session.add(
            AllianceMember(
                country_id=nation.id, alliance_id=alliance.id, money_contributed=0, resource_contributions={}
            )
        )
        return f"Создан союз {name}, ID {alliance.id}. Вместимость: {self.cfg['max_members']} стран."

    async def join(self, tg: int, alliance_id: int, invitation_id: int | None = None) -> str:
        nation = await country(self.session, tg, lock=True)
        if not nation.independent:
            raise GameError("В союзы могут вступать только независимые страны.")
        if await self.session.get(AllianceMember, nation.id):
            raise GameError("Вы уже состоите в союзе.")
        alliance = (await self._lock_alliances([alliance_id])).get(alliance_id)
        if alliance is None or alliance.disbanded:
            raise GameError("Такого действующего союза нет.")
        invite = (
            await self.session.scalar(
                select(AllianceInvitation).where(AllianceInvitation.id == invitation_id).with_for_update()
            )
            if invitation_id
            else None
        )
        if (
            not invite
            or invite.country_id != nation.id
            or invite.alliance_id != alliance.id
            or invite.answered_at
            or aware(invite.expires_at) <= utcnow()
            or invite.inviter_id != alliance.leader_country_id
        ):
            raise GameError("Нужно действующее приглашение главы союза.")
        await ensure_alliance_peace(self.session, alliance.id)
        count = await self.session.scalar(
            select(func.count()).select_from(AllianceMember).where(AllianceMember.alliance_id == alliance_id)
        )
        if count >= self.cfg["max_members"]:
            raise GameError("В союзе нет свободных мест.")
        self.session.add(
            AllianceMember(
                country_id=nation.id, alliance_id=alliance.id, money_contributed=0, resource_contributions={}
            )
        )
        invite.answered_at = utcnow()
        return f"Вы вступили в союз {alliance.name}."

    async def invite(self, tg: int, target_tg: int):
        actor = await country(self.session, tg, lock=False)
        target = await country(self.session, target_tg, lock=False)
        await self.session.execute(
            select(Country.id)
            .where(Country.id.in_(sorted({actor.id, target.id})))
            .order_by(Country.id)
            .with_for_update(key_share=True)
        )
        nation, _, alliance = await self._context(tg, leader=True)
        if target.id == nation.id or not target.independent or await self.session.get(AllianceMember, target.id):
            raise GameError("Игрок должен быть независимым и не состоять в союзе.")
        count = await self.session.scalar(
            select(func.count()).select_from(AllianceMember).where(AllianceMember.alliance_id == alliance.id)
        )
        if count >= self.cfg["max_members"]:
            raise GameError("В союзе нет свободных мест.")
        now = utcnow()
        existing = await self.session.scalar(
            select(AllianceInvitation.id).where(
                AllianceInvitation.alliance_id == alliance.id,
                AllianceInvitation.country_id == target.id,
                AllianceInvitation.answered_at.is_(None),
                AllianceInvitation.expires_at > now,
            )
        )
        if existing:
            raise GameError("Игроку уже отправлено действующее приглашение.")
        invitation = AllianceInvitation(
            alliance_id=alliance.id,
            country_id=target.id,
            inviter_id=nation.id,
            expires_at=now + timedelta(hours=self.cfg["invitation_hours"]),
        )
        self.session.add(invitation)
        await self.session.flush()
        enqueue(
            self.session,
            target.telegram_id,
            f"Глава {nation.name} приглашает вас в союз {alliance.name}. Приглашение действует {self.cfg['invitation_hours']} ч.",
            [
                [
                    {"text": "Вступить", "data": f"i:{target.telegram_id}:{invitation.id}:yes"},
                    {"text": "Отклонить", "data": f"i:{target.telegram_id}:{invitation.id}:no"},
                ]
            ],
        )
        return "Приглашение отправлено."

    async def respond(self, tg: int, invitation_id: int, accept: bool):
        nation = await country(self.session, tg)
        invitation = await self.session.get(AllianceInvitation, invitation_id)
        if not invitation or invitation.country_id != nation.id:
            raise GameError("Это не ваше приглашение.")
        if accept:
            return await self.join(tg, invitation.alliance_id, invitation_id)
        invitation = await self.session.scalar(
            select(AllianceInvitation).where(AllianceInvitation.id == invitation_id).with_for_update()
        )
        if invitation.answered_at or aware(invitation.expires_at) <= utcnow():
            raise GameError("Приглашение уже недействительно.")
        invitation.answered_at = utcnow()
        return "Приглашение отклонено."

    async def leave(self, tg: int) -> str:
        nation, member, alliance = await self._context(tg, peaceful=False)
        if alliance.leader_country_id == nation.id:
            active = list(
                await self.session.scalars(
                    select(War)
                    .where(War.status == "active", or_(War.attacker_id == alliance.id, War.defender_id == alliance.id))
                    .order_by(War.id)
                    .with_for_update()
                )
            )
            for battle in active:
                battle.status = "cancelled"
                battle.resolved_at = utcnow()
                battle.report = f"Война отменена: союз {alliance.name} распущен главой."
                await self._notify(
                    [battle.attacker_id] + ([battle.defender_id] if battle.defender_id else []), battle.report
                )
            alliance.disbanded = True
            alliance.name_key = f"~{alliance.id}"
            alliance.money = 0
            await self.session.execute(delete(AllianceUnit).where(AllianceUnit.alliance_id == alliance.id))
            await self.session.execute(delete(AllianceInventory).where(AllianceInventory.alliance_id == alliance.id))
            await self.session.execute(delete(AllianceInvitation).where(AllianceInvitation.alliance_id == alliance.id))
            await self.session.execute(delete(AllianceMember).where(AllianceMember.alliance_id == alliance.id))
            return "Союз распущен. Все участники освобождены, техника и казна удалены."
        await ensure_alliance_peace(self.session, alliance.id)
        await self.session.execute(
            delete(AllianceUnit).where(
                AllianceUnit.alliance_id == alliance.id, AllianceUnit.contributor_country_id == nation.id
            )
        )
        await self.session.delete(member)
        return "Вы покинули союз. Ваша переданная техника уничтожена."

    async def transfer_leadership(self, tg: int, target_telegram_id: int) -> str:
        nation, _, alliance = await self._context(tg, leader=True)
        target = await self.session.scalar(select(Country).where(Country.telegram_id == target_telegram_id))
        if target is None or target.id == nation.id:
            raise GameError("Укажите Telegram ID другого участника союза.")
        target_member = await self.session.get(AllianceMember, target.id)
        if target_member is None or target_member.alliance_id != alliance.id:
            raise GameError("Получатель не состоит в вашем союзе.")
        alliance.leader_country_id = target.id
        return f"Новым главой союза стала страна {target.name}."

    async def contribute_units(self, tg: int, kind: str, quantity: int) -> str:
        self._quantity(quantity)
        self._unit(kind)
        nation, _, alliance = await self._context(tg)
        from game.services.country_war import ensure_equipment_available

        await ensure_equipment_available(self.session, nation.id)
        row = await self.session.get(Unit, (nation.id, kind))
        if row is None or row.amount < quantity:
            raise GameError("Недостаточно техники в армии страны.")
        row.amount -= quantity
        await self._add_unit(alliance.id, nation.id, kind, quantity)
        return f"В армию союза передано {number(quantity)} ед. техники. Вклад закреплён за вашей страной."

    async def payout(self, tg: int, target_telegram_id: int, quantity: int) -> str:
        self._quantity(quantity)
        # Country-first lock ordering also applies when paying a second country.
        ids = list(
            await self.session.scalars(
                select(Country.id).where(Country.telegram_id.in_([tg, target_telegram_id])).order_by(Country.id)
            )
        )
        await self.session.execute(
            select(Country.id).where(Country.id.in_(ids)).order_by(Country.id).with_for_update(key_share=True)
        )
        nation, _, alliance = await self._context(tg, leader=True)
        target = await self.session.scalar(select(Country).where(Country.telegram_id == target_telegram_id))
        if target is None:
            raise GameError("Страна получателя не найдена.")
        member = await self.session.get(AllianceMember, target.id)
        if member is None or member.alliance_id != alliance.id:
            raise GameError("Выдавать деньги можно только участникам вашего союза.")
        if alliance.money < quantity:
            raise GameError("В общаке недостаточно денег.")
        alliance.money -= quantity
        target.money += quantity
        self.session.add(GameEvent(country_id=target.id, text=f"Союз {alliance.name} выплатил вам {money(quantity)}."))
        return f"Стране {target.name} выдано {money(quantity)}."

    async def start_war(self, tg: int) -> str:
        nation = await country(self.session, tg, lock=True)
        member = await self._membership(nation.id)
        own_id = member.alliance_id
        now = utcnow()
        own_power, _ = await self._equipment(own_id)
        if own_power < self.cfg["war_min_power"]:
            raise GameError(f"Для войны требуется мощь союза не ниже {self.cfg['war_min_power']}.")
        power_query = (
            select(
                AllianceUnit.alliance_id.label("aid"),
                func.sum(self._power_expression()).label("power"),
            )
            .group_by(AllianceUnit.alliance_id)
            .subquery()
        )
        active_war = exists(
            select(War.id).where(
                War.status == "active",
                or_(War.attacker_id == Alliance.id, War.defender_id == Alliance.id),
            )
        )
        tolerance = self.cfg["match_power_tolerance"]
        candidate_id = await self.session.scalar(
            select(Alliance.id)
            .join(
                power_query,
                power_query.c.aid == Alliance.id,
            )
            .where(
                Alliance.id != own_id,
                Alliance.disbanded.is_(False),
                or_(Alliance.war_ready_at.is_(None), Alliance.war_ready_at <= now),
                ~active_war,
                power_query.c.power >= self.cfg["war_min_power"],
                power_query.c.power.between(own_power * (1 - tolerance), own_power * (1 + tolerance)),
            )
            .order_by(func.random())
            .limit(1)
        )
        locked = await self._lock_alliances([own_id] + ([candidate_id] if candidate_id else []))
        attacker = locked[own_id]
        if attacker.leader_country_id != nation.id:
            raise GameError("Начать войну может только глава союза.")
        await ensure_alliance_peace(self.session, own_id)
        if attacker.war_ready_at and aware(attacker.war_ready_at) > now:
            raise GameError(f"Следующая война доступна {remaining(aware(attacker.war_ready_at))}.")
        # All matching predicates are checked again after acquiring row locks.
        own_power, equipment_value = await self._equipment(own_id)
        if own_power < self.cfg["war_min_power"]:
            raise GameError("Недостаточно военной мощи союза.")
        defender = locked.get(candidate_id)
        defender_power = 0
        if defender is not None:
            busy = await self.session.scalar(
                select(War.id)
                .where(
                    War.status == "active",
                    or_(War.attacker_id == defender.id, War.defender_id == defender.id),
                )
                .limit(1)
            )
            defender_power, _ = await self._equipment(defender.id)
            if (
                defender.disbanded
                or busy is not None
                or (defender.war_ready_at and aware(defender.war_ready_at) > now)
                or not own_power * (1 - tolerance) <= defender_power <= own_power * (1 + tolerance)
                or defender_power < self.cfg["war_min_power"]
            ):
                defender = None
        if defender is None:
            defender_power = max(1, round(own_power * self.rng.uniform(1 - tolerance, 1 + tolerance)))
            opponent_name = self.rng.choice(self.cfg["npc_names"])
        else:
            opponent_name = defender.name
        ends_at = now + timedelta(hours=self.cfg["war_duration_hours"])
        ready_at = ends_at + timedelta(hours=self.cfg["war_cooldown_hours"])
        attacker.war_ready_at = ready_at
        if defender:
            defender.war_ready_at = ready_at
        war = War(
            attacker_id=attacker.id,
            defender_id=defender.id if defender else None,
            opponent_name=opponent_name,
            attacker_power=own_power,
            defender_power=defender_power,
            stake=equipment_value + ((await self._equipment(defender.id))[1] if defender else equipment_value),
            started_at=now,
            ends_at=ends_at,
            status="active",
            report="",
        )
        self.session.add(war)
        await self.session.flush()
        await self._notify(
            [attacker.id] + ([defender.id] if defender else []),
            f"Началась война: {attacker.name} против {opponent_name}. Завершение: {remaining(ends_at)}.",
        )
        return f"Война: {attacker.name} против {opponent_name}. Силы {number(own_power)} / {number(defender_power)}. Итог через {self.cfg['war_duration_hours']} ч."

    async def _notify(self, alliance_ids: list[int], message: str):
        from sqlalchemy import insert, literal

        await self.session.execute(
            insert(GameEvent).from_select(
                ["country_id", "text", "created_at"],
                select(AllianceMember.country_id, literal(message), literal(utcnow())).where(
                    AllianceMember.alliance_id.in_(alliance_ids)
                ),
            )
        )

    async def resolve_due_wars(self, now: datetime | None = None) -> int:
        """Resolve a bounded batch. Safe to call from multiple worker processes."""
        now = aware(now) if now else utcnow()
        due = (
            await self.session.execute(
                select(War.id, War.attacker_id, War.defender_id)
                .where(
                    War.status == "active",
                    War.ends_at <= now,
                )
                .order_by(War.ends_at, War.id)
                .limit(self.cfg["resolve_batch_size"])
            )
        ).all()
        # A batch shares one transaction: lock its complete alliance set once so
        # later wars cannot invert the lock order established by earlier wars.
        locked_alliances = (
            await self._lock_alliances(
                [aid for _, attacker_id, defender_id in due for aid in (attacker_id, defender_id) if aid is not None]
            )
            if due
            else {}
        )
        resolved = 0
        for war_id, attacker_id, defender_id in due:
            ids = [attacker_id] + ([defender_id] if defender_id else [])
            alliances = locked_alliances
            war = await self.session.scalar(
                select(War)
                .where(War.id == war_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if war is None or war.status != "active":
                continue
            attacker = alliances[attacker_id]
            defender = alliances.get(defender_id)
            probability = max(
                self.cfg["win_probability_min"],
                min(
                    self.cfg["win_probability_max"],
                    war.attacker_power / max(1, war.attacker_power + war.defender_power),
                ),
            )
            attacker_wins = self.rng.random() < probability
            winner = attacker if attacker_wins else defender
            loser = defender if attacker_wins else attacker
            reward = 0
            destroyed = 0
            if loser is not None:
                for row in await self.session.scalars(select(AllianceUnit).where(AllianceUnit.alliance_id == loser.id)):
                    kept = math.floor(row.amount * self.cfg["loser_retention"])
                    destroyed += row.amount - kept
                    row.amount = kept
            if winner is not None:
                reward = math.floor(war.stake * self.cfg["reward_multiplier"])
                for row in await self.session.scalars(
                    select(AllianceUnit).where(AllianceUnit.alliance_id == winner.id)
                ):
                    row.amount = 0
            if winner is not None:
                winner.money += reward
            winner_name = winner.name if winner is not None else war.opponent_name
            war.status = "resolved"
            war.resolved_at = now
            war.winner_id = winner.id if winner is not None else None
            war.reward = reward
            war.report = f"Война завершена. Победитель: {winner_name}. Награда: {money(reward)}. Потери проигравшего: {number(destroyed)} ед. техники."
            await self._notify(ids, war.report)
            resolved += 1
        return resolved

    async def snapshot(self, tg: int) -> dict | None:
        nation = await country(self.session, tg, lock=True)
        member = await self.session.get(AllianceMember, nation.id)
        if member is None:
            return None
        alliance = (await self._lock_alliances([member.alliance_id]))[member.alliance_id]
        equipment = list(
            await self.session.scalars(select(AllianceUnit).where(AllianceUnit.alliance_id == alliance.id))
        )
        power_by_member: dict[int, int] = {}
        units: dict[str, int] = {}
        for row in equipment:
            power_by_member[row.contributor_country_id] = (
                power_by_member.get(row.contributor_country_id, 0) + row.amount * self._unit(row.kind)["power"]
            )
            units[row.kind] = units.get(row.kind, 0) + row.amount
        total_power = sum(power_by_member.values())
        rows = (
            await self.session.execute(
                select(AllianceMember, Country)
                .join(Country, Country.id == AllianceMember.country_id)
                .where(AllianceMember.alliance_id == alliance.id)
                .order_by(Country.id)
            )
        ).all()
        members = [
            {
                "country_id": c.id,
                "telegram_id": c.telegram_id,
                "name": c.name,
                "power": power_by_member.get(c.id, 0),
                "power_percent": power_by_member.get(c.id, 0) / total_power * 100 if total_power else 0,
            }
            for m, c in rows
        ]
        leader_tg = next(m["telegram_id"] for m in members if m["country_id"] == alliance.leader_country_id)
        war = await self.session.scalar(
            select(War)
            .where(
                or_(War.attacker_id == alliance.id, War.defender_id == alliance.id),
            )
            .order_by(War.id.desc())
            .limit(1)
        )
        war_data = None
        if war:
            opponent = war.opponent_name
            if war.defender_id == alliance.id:
                opponent = await self.session.scalar(select(Alliance.name).where(Alliance.id == war.attacker_id))
            war_data = {
                "id": war.id,
                "opponent": opponent,
                "status": war.status,
                "ends_at": aware(war.ends_at),
                "report": war.report,
                "attacker_power": war.attacker_power,
                "defender_power": war.defender_power,
                "simulated": war.defender_id is None,
                "reward": war.reward,
                "won": war.winner_id == alliance.id,
            }
        return {
            "id": alliance.id,
            "name": alliance.name,
            "leader_id": alliance.leader_country_id,
            "leader_telegram_id": leader_tg,
            "money": alliance.money,
            "power": total_power,
            "members": members,
            "inventory": {
                r.resource: r.amount
                for r in await self.session.scalars(
                    select(AllianceInventory).where(AllianceInventory.alliance_id == alliance.id)
                )
            },
            "units": units,
            "war": war_data,
            "war_ready_at": aware(alliance.war_ready_at) if alliance.war_ready_at else None,
        }

    async def rankings(self, tg: int | None = None) -> list[dict]:
        power = (
            select(AllianceUnit.alliance_id.label("aid"), func.sum(self._power_expression()).label("power"))
            .group_by(AllianceUnit.alliance_id)
            .subquery()
        )
        members = (
            select(AllianceMember.alliance_id.label("aid"), func.count().label("members"))
            .group_by(AllianceMember.alliance_id)
            .subquery()
        )
        rows = (
            (
                await self.session.execute(
                    select(
                        Alliance.id,
                        Alliance.name,
                        Alliance.money,
                        func.coalesce(power.c.power, 0).label("power"),
                        func.coalesce(members.c.members, 0).label("members"),
                    )
                    .outerjoin(power, power.c.aid == Alliance.id)
                    .outerjoin(members, members.c.aid == Alliance.id)
                    .where(
                        Alliance.disbanded.is_(False),
                    )
                    .order_by(func.coalesce(power.c.power, 0).desc(), Alliance.money.desc(), Alliance.id)
                    .limit(self.cfg["ranking_limit"])
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
