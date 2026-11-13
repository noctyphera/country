"""Private numbered escrow offers; all mutations lock accounts before assets."""

from __future__ import annotations
import math
from game.formatting import number, money

from datetime import timedelta
from sqlalchemy import func, select
from game.core import GameError, aware, country, positive_quantity
from game.models import Country, Inventory, MarketOrder, TransferLog, Unit, GameEvent, utcnow
from game.notifications import enqueue


class MarketService:
    def __init__(self, session, rules):
        self.session, self.rules = session, rules

    def _resource(self, key):
        if key in self.rules["units"]:
            return dict(self.rules["units"][key], price=self.rules["market"]["unit_prices"][key])
        cfg = self.rules["resources"].get(key)
        if not cfg or not cfg["tradable"]:
            raise GameError("Этот предмет не продаётся на обычном рынке.")
        return cfg

    async def _balance(self, owner, key):
        cls = Unit if key in self.rules["units"] else Inventory
        row = await self.session.get(cls, (owner.id, key))
        if row is None:
            row = cls(country_id=owner.id, **{"kind" if cls is Unit else "resource": key}, amount=0)
            self.session.add(row)
        return row

    async def orders(self, telegram_id, page=0):
        owner = await country(self.session, telegram_id, lock=False)
        if not isinstance(page, int) or page < 0:
            raise GameError("Некорректная страница.")
        rows = await self.session.execute(
            select(MarketOrder.id, MarketOrder.resource, MarketOrder.amount, MarketOrder.price)
            .where(MarketOrder.country_id == owner.id, MarketOrder.amount > 0)
            .order_by(MarketOrder.id)
            .offset(page * self.rules["market"]["page_size"])
            .limit(self.rules["market"]["page_size"])
        )
        return [dict(r) for r in rows.mappings()]

    async def create_order(self, telegram_id, resource, quantity):
        positive_quantity(quantity, self.rules)
        cfg = self._resource(resource)
        owner = await country(self.session, telegram_id)
        count = await self.session.scalar(
            select(func.count())
            .select_from(MarketOrder)
            .where(MarketOrder.country_id == owner.id, MarketOrder.amount > 0)
        )
        if count >= self.rules["market"]["max_open_orders"]:
            raise GameError("Достигнут лимит открытых лотов.")
        if resource in self.rules["units"]:
            from game.services.country_war import ensure_equipment_available

            await ensure_equipment_available(self.session, owner.id)
        stock = await self._balance(owner, resource)
        if stock.amount < quantity:
            raise GameError("Недостаточно товара.")
        stock.amount -= quantity
        order = MarketOrder(country_id=owner.id, resource=resource, amount=quantity, price=cfg["price"])
        self.session.add(order)
        await self.session.flush()
        return f"Лот №{order.id}: {cfg['name']} × {number(quantity)}. Цена {money(cfg['price'])} за единицу. Сообщите покупателю номер лота."

    async def buy_order(self, telegram_id, order_id, quantity=None):
        order = await self.session.get(MarketOrder, order_id)
        if not order or not order.country_id or order.amount <= 0:
            raise GameError("Лот не найден или закрыт.")
        actor = await country(self.session, telegram_id, lock=False)
        ids = sorted({actor.id, order.country_id})
        owners = {
            c.id: c
            for c in await self.session.scalars(
                select(Country)
                .where(Country.id.in_(ids))
                .order_by(Country.id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        }
        order = await self.session.scalar(
            select(MarketOrder)
            .where(MarketOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        quantity = order.amount if quantity is None else quantity
        positive_quantity(quantity, self.rules)
        if order.country_id == actor.id:
            raise GameError("Это ваш лот.")
        if order.amount < quantity:
            raise GameError("В лоте недостаточно товара.")
        total = order.price * quantity
        buyer, seller = owners[actor.id], owners[order.country_id]
        if buyer.money < total:
            raise GameError(f"Недостаточно денег: нужно {number(total)}.")
        buyer.money -= total
        seller.money += total
        (await self._balance(buyer, order.resource)).amount += quantity
        order.amount -= quantity
        notice = f"Лот №{order.id}: {self._resource(order.resource)['name']} × {number(quantity)} куплен страной {buyer.name} за {money(total)}."
        self.session.add(GameEvent(country_id=seller.id, text=notice))
        enqueue(self.session, seller.telegram_id, notice)
        return f"Куплен лот №{order.id}: {self._resource(order.resource)['name']} × {number(quantity)} за ¤{number(total)}."

    async def cancel_order(self, telegram_id, order_id):
        owner = await country(self.session, telegram_id)
        order = await self.session.scalar(select(MarketOrder).where(MarketOrder.id == order_id).with_for_update())
        if not order or order.country_id != owner.id or order.amount <= 0:
            raise GameError("Ваш открытый лот не найден.")
        (await self._balance(owner, order.resource)).amount += order.amount
        order.amount = 0
        return f"Лот №{order.id} снят, остаток возвращён."

    async def system_buy(self, telegram_id, resource, quantity):
        positive_quantity(quantity, self.rules)
        cfg = self._resource(resource)
        owner = await country(self.session, telegram_id)
        total = (
            math.ceil(
                cfg["price"]
                * self.rules["market"][
                    "unit_import_multiplier" if resource in self.rules["units"] else "system_buy_multiplier"
                ]
            )
            * quantity
        )
        if owner.money < total:
            raise GameError(f"Нужно ¤{number(total)}.")
        owner.money -= total
        (await self._balance(owner, resource)).amount += quantity
        return f"Импорт: {cfg['name']} × {number(quantity)}, стоимость ¤{number(total)}."

    async def system_sell(self, telegram_id, resource, quantity):
        positive_quantity(quantity, self.rules)
        cfg = self._resource(resource)
        owner = await country(self.session, telegram_id)
        if resource in self.rules["units"]:
            from game.services.country_war import ensure_equipment_available

            await ensure_equipment_available(self.session, owner.id)
        stock = await self._balance(owner, resource)
        if stock.amount < quantity:
            raise GameError("Недостаточно товара.")
        total = math.floor(
            cfg["price"]
            * quantity
            * self.rules["market"][
                "unit_export_multiplier" if resource in self.rules["units"] else "system_sell_multiplier"
            ]
        )
        if not total:
            raise GameError("Увеличьте объём экспорта.")
        stock.amount -= quantity
        owner.money += total
        return f"Экспорт: {cfg['name']} × {number(quantity)}, выручка ¤{number(total)}."

    async def transfer(self, telegram_id: int, target_id: int, amount: int) -> str:
        positive_quantity(amount, self.rules)
        if telegram_id == target_id:
            raise GameError("Нельзя перевести деньги самому себе.")
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
            raise GameError("Страна отправителя или получателя не найдена. Укажите Telegram ID игрока.")
        sender, recipient = by_tg[telegram_id], by_tg[target_id]
        cfg = self.rules["market"]
        now = utcnow()
        if any(aware(state.created_at) > now - timedelta(hours=cfg["transfer_min_age_hours"]) for state in states):
            raise GameError(f"Обеим странам должно быть не менее {cfg['transfer_min_age_hours']} часов.")
        window = now - timedelta(hours=cfg["transfer_window_hours"])
        sent = await self.session.scalar(
            select(func.coalesce(func.sum(TransferLog.amount), 0)).where(
                TransferLog.sender_id == sender.id, TransferLog.created_at >= window
            )
        )
        received = await self.session.scalar(
            select(func.coalesce(func.sum(TransferLog.amount), 0)).where(
                TransferLog.recipient_id == recipient.id, TransferLog.created_at >= window
            )
        )

        def daily_limit(state: Country) -> int:
            return min(
                cfg["transfer_daily_absolute"],
                state.population * self.rules["economy"]["tax_per_person"] * cfg["transfer_daily_tax_hours"],
            )

        remaining = min(
            daily_limit(sender) - sent,
            daily_limit(recipient) - received,
            int(sender.money * cfg["transfer_max_balance_share"]),
        )
        if amount > remaining:
            raise GameError(
                f"Лимит перевода сейчас: {max(0, remaining)}. Учитываются отправленные и полученные деньги за последние 24 часа."
            )
        fee = math.ceil(amount * cfg["transfer_fee_share"])
        if sender.money < amount + fee:
            raise GameError(f"Нужно {amount + fee}, включая комиссию {number(fee)}.")
        sender.money -= amount + fee
        recipient.money += amount
        self.session.add(TransferLog(sender_id=sender.id, recipient_id=recipient.id, amount=amount))
        return f"Переведено {money(amount)} в страну {recipient.name}. Комиссия: {money(fee)}."
