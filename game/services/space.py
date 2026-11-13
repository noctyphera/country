"""Lazy space expeditions: no timer or background process per player."""

from __future__ import annotations

import random
from game.formatting import number, remaining

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from game.core import GameError, add_resource, aware, country, positive_quantity, spend
from game.models import Expedition, GameEvent, utcnow
from game.services.country import require_independent, scaled_cost


class SpaceService:
    def __init__(self, session: AsyncSession, rules: dict[str, Any], rng: random.Random | None = None):
        self.session, self.rules = session, rules
        self.rng = rng or random.SystemRandom()

    async def craft_rocket(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        require_independent(state)
        cfg = self.rules["space"]
        await spend(self.session, state, money=cfg["rocket_money"], resources=cfg["rocket_cost"])
        await add_resource(self.session, state.id, "rocket", 1)
        return "Ракета построена. Можно отправить экспедицию из космической панели."

    async def launch(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        require_independent(state)
        cfg = self.rules["space"]
        now = utcnow()
        if await self.session.get(Expedition, state.id):
            raise GameError("Экспедиция уже идёт. Верните её через кнопку «Приземлить».")
        if state.space_ready_at and aware(state.space_ready_at) > now:
            raise GameError(f"Следующий запуск: {remaining(aware(state.space_ready_at))}.")
        await spend(self.session, state, resources={"rocket": 1})
        expedition = Expedition(
            country_id=state.id,
            launched_at=now,
            returns_at=now + timedelta(hours=cfg["duration_hours"]),
            destroyed=self.rng.random() < cfg["explosion_chance"],
        )
        self.session.add(expedition)
        return f"Экспедиция отправлена. После {remaining(expedition.returns_at)} нажмите «Приземлить» или /land."

    async def claim(self, telegram_id: int) -> str:
        state = await country(self.session, telegram_id)
        expedition = await self.session.get(Expedition, state.id)
        if not expedition:
            raise GameError("Нет активной экспедиции.")
        now = utcnow()
        if aware(expedition.returns_at) > now:
            raise GameError(f"Экспедиция вернётся после {remaining(aware(expedition.returns_at))}.")
        cfg = self.rules["space"]
        state.space_ready_at = now + timedelta(hours=cfg["cooldown_hours"])
        await self.session.delete(expedition)
        if expedition.destroyed:
            text = (
                "Связь с экспедицией потеряна. Ракета взорвалась, груз утрачен. Для нового полёта нужна новая ракета."
            )
        else:
            await add_resource(self.session, state.id, "rocket", 1)
            rewards = []
            for resource, bounds in cfg["rewards"].items():
                amount = self.rng.randint(*bounds)
                await add_resource(self.session, state.id, resource, amount)
                rewards.append(f"{self.rules['resources'][resource]['name']}: {number(amount)}")
            text = "Экспедиция вернулась, ракета готова к повторному использованию.\n" + "\n".join(rewards)
        self.session.add(GameEvent(country_id=state.id, text=text))
        return text + f"\nСледующий запуск через {cfg['cooldown_hours']} ч."

    async def craft_item(self, telegram_id: int, kind: str, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        cfg = self.rules["space"]["items"].get(kind)
        if not cfg:
            raise GameError("Неизвестный космический артефакт.")
        state = await country(self.session, telegram_id)
        require_independent(state)
        await spend(self.session, state, money=cfg["money"] * quantity, resources=scaled_cost(cfg["cost"], quantity))
        await add_resource(self.session, state.id, kind, quantity)
        return f"Создано: {cfg['name']} × {number(quantity)}."

    async def sell_item(self, telegram_id: int, kind: str, quantity: int) -> str:
        positive_quantity(quantity, self.rules)
        cfg = self.rules["space"]["items"].get(kind)
        if not cfg:
            raise GameError("Научный консорциум принимает только готовые космические артефакты.")
        state = await country(self.session, telegram_id)
        require_independent(state)
        await spend(self.session, state, resources={kind: quantity})
        total = cfg["sell_price"] * quantity
        state.money += total
        return f"Научный консорциум выкупил {cfg['name']} × {number(quantity)} за {number(total)}."
