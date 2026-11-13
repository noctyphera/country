"""Transactional inline controller with persistent forms and stale-button protection."""

from __future__ import annotations

import asyncio
import logging
from weakref import WeakValueDictionary
from datetime import timedelta
from html import escape, unescape

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, select

from game.bot.actions import ACTIONS
from game.bot.presentation import Callback, chunks, keyboard, label, number, ordered
from game.bot.views import format_cost, nav, render_page
from game.core import GameError
from game.models import Building, Country, MarketOrder, PendingInput, utcnow
from dataclasses import dataclass
from secrets import randbits
from game.core import aware
from game.services.alliance import AllianceService
from game.services.country import GameService
from game.services.intelligence import IntelligenceService
from game.services.market import MarketService
from game.services.space import SpaceService
from game.services.country_war import CountryWarService
from game.formatting import message_numbers

log = logging.getLogger(__name__)


def valid_owner(payload: Callback, user_id: int) -> bool:
    return payload.owner == user_id


@dataclass
class MenuContext:
    telegram_id: int
    chat_id: int
    message_id: int
    nonce: int = 0
    page: str = "home"


def parse_input(raw: str, kind: str, rules: dict):
    raw = raw.strip()
    if len(raw) > rules["ui"]["max_input_length"]:
        raise GameError("Введённое значение слишком длинное.")
    if kind in {"int", "id"}:
        if not raw.isascii() or not raw.isdecimal():
            raise GameError("Нужно целое положительное число без пробелов и знаков.")
        value = int(raw)
        maximum = rules["ui"]["max_input_id" if kind == "id" else "max_input_integer"]
        if not 0 < value <= maximum:
            raise GameError(f"Введите число от 1 до {maximum}.")
        return value
    if not raw:
        raise GameError("Введите непустое название.")
    return raw


def catalog(kind: str, rules: dict) -> dict:
    if kind == "item":
        return rules["space"]["items"]
    if kind == "asset":
        return {**{k: v for k, v in rules["resources"].items() if v.get("tradable")}, **rules["units"]}
    if kind == "resource":
        return {key: value for key, value in rules["resources"].items() if value.get("tradable", True)}
    return rules[{"building": "buildings", "unit": "units"}[kind]]


def operation_description(code: str, args: list, rules: dict) -> str:
    spec = ACTIONS[code]
    lines = [f"<b>{escape(spec.title)}</b>"]
    if args:
        chosen = []
        for index, value in enumerate(args):
            field = spec.fields[index] if index < len(spec.fields) else None
            if field and (field.kind == "id" or (index == 0 and code in {"buy", "cancel", "ajoin"})):
                chosen.append("<code>" + escape(str(value)) + "</code>")
            else:
                chosen.append(label(value, rules) if isinstance(value, str) else number(value))
        lines.append("Выбрано: " + ", ".join(chosen))
    if code in {"build", "craft", "aunit", "acraft", "item", "itemsell"} and args:
        section = "buildings" if code == "build" else "units"
        item = (
            rules["space"]["items"].get(str(args[0]))
            if code in {"item", "itemsell"}
            else rules[section].get(str(args[0]))
        )
        if item:
            if code == "itemsell":
                lines.append(f"Цена продажи за единицу: ¤ {number(item['sell_price'])}")
            elif code != "aunit":
                lines.append("Цена за единицу: " + format_cost(item, rules))
            if "power" in item:
                lines.append(f"Мощность единицы: {item['power']}")
            if "land" in item:
                lines.append(f"Занимает территории: {item['land']}")
    if code in {"sell", "asell", "sysbuy", "syssell", "asysbuy", "asyssell"} and args:
        from math import ceil

        price = rules["market"]["unit_prices"].get(
            str(args[0]), rules["resources"].get(str(args[0]), {}).get("price", 0)
        )
        if "sysbuy" in code:
            price = ceil(
                price
                * rules["market"][
                    "unit_import_multiplier" if str(args[0]) in rules["units"] else "system_buy_multiplier"
                ]
            )
        if "syssell" in code:
            price = (
                price
                * rules["market"][
                    "unit_export_multiplier" if str(args[0]) in rules["units"] else "system_sell_multiplier"
                ]
            )
        lines.append(f"Цена единицы: ¤ {number(price)}")
    if code == "recruit":
        cfg = rules["recruitment"]
        lines.append(
            f"На солдата: ¤ {cfg['money_per_soldier']}, продовольствие {cfg['food_per_soldier']}. Лимит армии: {cfg['maximum_share']:.0%} всех жителей."
        )
    if code == "feed":
        lines.append("Еда берётся со склада, повышает сытость; излишек остаётся в резерве.")
    if code == "expand":
        cfg = rules["expansion"]
        lines.append(
            f"Один участок: {cfg['land_per_purchase']} территории. Базовая цена: ¤ {cfg['money']}, дерево {cfg['wood']}, камень {cfg['stone']}. Цена растёт с площадью страны."
        )
    if code == "rocket":
        cfg = rules["space"]
        lines.append("Цена ракеты: " + format_cost({"money": cfg["rocket_money"], "cost": cfg["rocket_cost"]}, rules))
    if code == "launch":
        cfg = rules["space"]
        lines.append(
            f"Срок: {cfg['duration_hours']} ч. Вероятность потери ракеты: {cfg['explosion_chance']:.0%}. После возвращения: {cfg['cooldown_hours']} ч. до следующего запуска."
        )
    if code == "send":
        cfg = rules["market"]
        lines.append(
            f"Комиссия: {cfg['transfer_fee_share']:.0%}. Суточный предел: до ¤ {cfg['transfer_daily_absolute']}, также ограничен доходом с налогов; за один раз — не более {cfg['transfer_max_balance_share']:.0%} баланса."
        )
    if code == "indep":
        cfg = rules["independence"]
        lines.append(
            f"Необходимы: население {cfg['population']}, мощь {cfg['military_power']}, ВВП на жителя {cfg['gdp']}. Сбор: ¤ {cfg['money']}."
        )
    if code in {"sup", "hup", "spies"}:
        cfg = rules["intelligence"]
        if code == "spies":
            lines.append(
                f"На разведчика: один военнослужащий, ¤ {cfg['spy_training_money']}, еда {cfg['spy_training_food']}."
            )
        else:
            prefix = "spy" if code == "sup" else "hack"
            lines.append(
                "Базовая цена: "
                + format_cost({"money": cfg[prefix + "_research_money"], "cost": cfg[prefix + "_research_cost"]}, rules)
                + ". Стоимость растёт с уровнем."
            )
    if code in {"eup", "efix"}:
        cfg = rules["energy"]
        prefix = "upgrade" if code == "eup" else "maintenance"
        lines.append(
            "Базовая цена: " + format_cost({"money": cfg[prefix + "_money"], "cost": cfg[prefix + "_cost"]}, rules)
        )
        lines.append("Обслуживание учитывает число станций; модернизация — текущий уровень сети.")
    if code == "cwar":
        cfg = rules["country_war"]
        lines.append(
            f"Случайная цель по мощи ±{number(cfg['power_tolerance'] * 100)}%. Бой {number(cfg['duration_hours'])} ч. Добыча: {number(cfg['loot_share'] * 100)}% денег и обычных ресурсов на момент завершения. Потери обеих сторон около {number(cfg['loss_share'] * 100)}%. Щит проигравшего {number(cfg['shield_hours'])} ч.; отдых победителя {number(cfg['cooldown_hours'])} ч. Разница сил даёт лишь небольшое преимущество."
        )
    if code == "war":
        lines.append(
            "Глава принимает условия войны. Подходящий союз подбирается автоматически, при его отсутствии проводится бой с учебным соперником. Возможны потери техники."
        )
    return "\n\n".join(lines)


def form_view(pending: PendingInput, rules: dict):
    action = ACTIONS[pending.action]
    args = pending.payload.get("args", [])
    text = operation_description(pending.action, args, rules)
    if pending.payload.get("quote"):
        text += "\n\n<b>Расчёт для вашей страны:</b>\n" + pending.payload["quote"]
    rows = []
    if len(args) < len(action.fields):
        field = action.fields[len(args)]
        text += "\n\n" + escape(field.prompt)
        if field.kind in {"resource", "asset", "building", "unit", "item"}:
            entries = catalog(field.kind, rules)
            if field.kind == "building":
                entries = {
                    k: v for k, v in entries.items() if not v.get("independent") or pending.payload.get("independent")
                }
            rows += chunks(
                [(str(item.get("name", key)), "v", key) for key in ordered(entries, rules) for item in [entries[key]]]
            )
    else:
        text += "\n\nПодтвердите действие."
        rows += [[("✓ Подтвердить", "yes", "")]]
    rows += nav(pending.payload.get("return_page", action.page))
    return text, rows


async def execute_action(session, rules: dict, telegram_id: int, code: str, args: list) -> str:
    action = ACTIONS[code]
    services = {
        "game": GameService,
        "market": MarketService,
        "intel": IntelligenceService,
        "space": SpaceService,
        "countrywar": CountryWarService,
        "alliance": AllianceService,
    }
    service = services[action.service](session, rules)
    async with session.begin_nested():
        result = await getattr(service, action.method)(telegram_id, *args)
        await session.flush()
    return str(result)


async def _delete_prompt(session, telegram_id: int):
    await session.execute(delete(PendingInput).where(PendingInput.telegram_id == telegram_id))


async def _pending(session, panel):
    pending = await session.get(PendingInput, panel.telegram_id)
    if pending and aware(pending.expires_at) <= utcnow():
        await session.delete(pending)
        return None
    if pending and (
        pending.chat_id != panel.chat_id
        or pending.message_id != panel.message_id
        or pending.payload.get("nonce") != panel.nonce
    ):
        return None
    return pending


async def _new_prompt(session, rules: dict, panel: MenuContext, code: str, args: list):
    await _delete_prompt(session, panel.telegram_id)
    state = await session.scalar(select(Country).where(Country.telegram_id == panel.telegram_id))
    panel.nonce = randbits(32)
    pending = PendingInput(
        telegram_id=panel.telegram_id,
        action=code,
        payload={
            "nonce": panel.nonce,
            "args": args,
            "return_page": "energy"
            if code == "build" and args and args[0] in {"solar", "nuclear"}
            else ACTIONS[code].page,
            "independent": bool(state and state.independent),
        },
        expires_at=utcnow() + timedelta(seconds=rules["ui"]["input_timeout_seconds"]),
        chat_id=panel.chat_id,
        message_id=panel.message_id,
    )
    if code in {"feed", "civilfood"}:
        pending.payload = {**pending.payload, "quote": await current_quote(session, rules, panel.telegram_id, code, [])}
    session.add(pending)
    return pending


async def current_quote(session, rules: dict, telegram_id: int, code: str, args: list) -> str:
    """Read-only preview; domain services revalidate funds at confirmation."""
    state = await session.scalar(select(Country).where(Country.telegram_id == telegram_id))
    if state is None:
        return ""
    cost, money = {}, None
    if code in {"sup", "hup"}:
        cfg = rules["intelligence"]
        prefix = "spy" if code == "sup" else "hack"
        factor = state.spy_level if code == "sup" else state.hacker_level + 1
        money = cfg[prefix + "_research_money"] * factor
        cost = {key: amount * factor for key, amount in cfg[prefix + "_research_cost"].items()}
    elif code in {"eup", "efix"}:
        cfg = rules["energy"]
        if code == "eup":
            factor = state.energy_level
        else:
            plants = (
                await session.execute(select(Building.kind, Building.amount).where(Building.country_id == state.id))
            ).all()
            factor = sum(cfg["maintenance_station_weights"].get(kind, 0) * count for kind, count in plants)
        prefix = "upgrade" if code == "eup" else "maintenance"
        money = cfg[prefix + "_money"] * factor
        cost = {key: amount * factor for key, amount in cfg[prefix + "_cost"].items()}
    elif code == "expand" and args:
        from game.core import positive_quantity

        positive_quantity(args[0], rules)
        cfg = rules["expansion"]
        factor = sum(
            1 + (state.land + index * cfg["land_per_purchase"]) // cfg["scaling_land"] for index in range(args[0])
        )
        money = cfg["money"] * factor
        cost = {key: cfg[key] * factor for key in ("wood", "stone")}
    elif code == "buy" and args:
        order = await session.get(MarketOrder, args[0])
        if order:
            if order.amount <= 0 or not order.country_id:
                raise GameError("Лот закрыт.")
            return f"Лот №{order.id}: {label(order.resource, rules)} × {number(order.amount)}. Цена {number(order.price)} за ед. Итого: ¤{number(order.price * order.amount)}."
    elif code == "send" and len(args) == 2:
        from math import ceil

        money = args[1] + ceil(args[1] * rules["market"]["transfer_fee_share"])
    elif code == "acreate":
        money = rules["alliance"]["creation_money"]
    if code in {"feed", "civilfood"}:
        import math

        people = state.soldiers + state.spies if code == "feed" else state.population
        rate = rules["economy"]["food_per_percent_army" if code == "feed" else "food_per_percent_civilian"]
        return (
            f"Для +1% сытости нужно {number(max(1, math.ceil(people * rate)))} ед. еды. Излишек сохраняется в резерве."
        )
    if code == "eup":
        return (
            "Цена: "
            + format_cost({"money": money, "cost": cost}, rules)
            + f"\nУровень {state.energy_level} → {state.energy_level + 1}; выработка +{number(rules['energy']['level_bonus'] * 100)}% базовой мощности."
        )
    if money is None:
        return ""
    return "Итого: " + format_cost({"money": money, "cost": cost}, rules)


async def _finish_or_prompt(session, rules: dict, panel: MenuContext, pending: PendingInput, *, confirm: bool = False):
    action = ACTIONS[pending.action]
    args = pending.payload["args"]
    if len(args) < len(action.fields) or (action.confirm and not confirm):
        if len(args) == len(action.fields):
            quote = await current_quote(session, rules, panel.telegram_id, pending.action, args)
            pending.payload = {**pending.payload, "args": args, "quote": quote}
        return (*form_view(pending, rules), "")
    try:
        notice = await execute_action(session, rules, panel.telegram_id, pending.action, args)
    except GameError as exc:
        notice = str(exc)
    await _delete_prompt(session, panel.telegram_id)
    panel.page = pending.payload.get("return_page", action.page)
    text, rows = await render_page(session, rules, panel.telegram_id, panel.page)
    return text, rows, notice


async def _publish(bot, db, rules, context, text, rows, notice=""):
    owner, chat_id, message_id, nonce = context
    if notice:
        text = escape(notice) + "\n\n" + text
    text = message_numbers(text)
    if len(text) > rules["ui"]["message_limit"]:
        import re

        text = escape(unescape(re.sub(r"<[^>]+>", "", text))[: rules["ui"]["message_limit"]]) + "…"
    markup = keyboard(owner, nonce, rows)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        if "message to edit not found" in str(exc) or "message can't be edited" in str(exc):
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        else:
            raise
    except TelegramForbiddenError:
        log.info("Chat unavailable for %s", owner)


class UserSerialMiddleware(BaseMiddleware):
    def __init__(self):
        self.locks = WeakValueDictionary()

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await handler(event, data)


def context_data(c):
    return c.telegram_id, c.chat_id, c.message_id, c.nonce


def make_router(db, rules):
    router = Router(name="country-menu")
    serial = UserSerialMiddleware()
    router.message.outer_middleware(serial)
    router.callback_query.outer_middleware(serial)

    @router.message(CommandStart())
    @router.message(Command("panel", "menu", "create", "cancel", "launch", "land", "rank", "unlist"))
    @router.message(F.text.strip().casefold().in_({"рейтинг", "/рейтинг"}))
    async def open_panel(message: Message, bot: Bot):
        if not message.from_user:
            return
        owner = message.from_user.id
        placeholder = await message.answer("🌍 Меню страны")
        ctx = MenuContext(owner, message.chat.id, placeholder.message_id)
        command = (message.text or "").split(maxsplit=1)
        name = command[0].split("@")[0].lstrip("/").casefold()
        notice = ""
        async with db.sessions() as session, session.begin():
            if name == "cancel":
                await _delete_prompt(session, owner)
            if name in {"rank", "рейтинг"}:
                text, rows = await render_page(session, rules, owner, "rank")
            elif name == "create":
                if len(command) > 1:
                    try:
                        notice = await execute_action(session, rules, owner, "create", [command[1]])
                    except GameError as exc:
                        notice = str(exc)
                    text, rows = await render_page(session, rules, owner, "home")
                else:
                    pending = await _new_prompt(session, rules, ctx, "create", [])
                    text, rows = form_view(pending, rules)
            elif name in {"launch", "land"}:
                pending = await _new_prompt(session, rules, ctx, "launch" if name == "launch" else "claim", [])
                text, rows, notice = await _finish_or_prompt(session, rules, ctx, pending)
            elif name == "unlist":
                try:
                    notice = await execute_action(
                        session,
                        rules,
                        owner,
                        "cancel",
                        [parse_input(command[1] if len(command) > 1 else "", "int", rules)],
                    )
                except GameError as exc:
                    notice = str(exc)
                text, rows = await render_page(session, rules, owner, "market")
            else:
                text, rows = await render_page(session, rules, owner, "home")
        await _publish(bot, db, rules, context_data(ctx), text, rows, notice)

    @router.callback_query(F.data.startswith("i:"))
    async def invitation_callback(query: CallbackQuery, bot: Bot):
        try:
            _, owner, invitation, choice = query.data.split(":")
            if int(owner) != query.from_user.id:
                await query.answer("Это не ваше приглашение.", show_alert=True)
                return
            if choice not in {"yes", "no"}:
                raise ValueError
            async with db.sessions() as session, session.begin():
                notice = await AllianceService(session, rules).respond(
                    query.from_user.id, int(invitation), choice == "yes"
                )
        except (ValueError, GameError) as exc:
            await query.answer(str(exc) or "Недействительное приглашение.", show_alert=True)
            return
        await query.answer(notice, show_alert=True)
        if isinstance(query.message, Message):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None
                )
            except TelegramBadRequest:
                pass

    @router.callback_query(F.data.startswith("c:"))
    async def handle_callback(query: CallbackQuery, bot: Bot):
        try:
            payload = Callback.unpack(query.data or "")
        except (ValueError, TypeError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        if not valid_owner(payload, query.from_user.id):
            await query.answer("Это не ваша кнопка.", show_alert=True)
            return
        if not isinstance(query.message, Message):
            await query.answer("Откройте /menu.", show_alert=True)
            return
        ctx = MenuContext(payload.owner, query.message.chat.id, query.message.message_id, payload.nonce)
        notice = ""
        async with db.sessions() as session, session.begin():
            try:
                if payload.operation == "p":
                    pending = await session.get(PendingInput, ctx.telegram_id)
                    if pending and pending.chat_id == ctx.chat_id and pending.message_id == ctx.message_id:
                        await _delete_prompt(session, ctx.telegram_id)
                    text, rows = await render_page(session, rules, ctx.telegram_id, payload.argument)
                elif payload.operation == "a":
                    code, _, preset = payload.argument.partition("~")
                    if code not in ACTIONS:
                        raise GameError("Действие недоступно.")
                    args = []
                    if preset:
                        field = ACTIONS[code].fields[0]
                        if field.kind in {"int", "id"}:
                            args = [parse_input(preset, field.kind, rules)]
                        elif preset in catalog(field.kind, rules):
                            args = [preset]
                        else:
                            raise GameError("Неизвестный вариант.")
                    pending = await _new_prompt(session, rules, ctx, code, args)
                    text, rows, notice = await _finish_or_prompt(session, rules, ctx, pending)
                elif payload.operation in {"v", "yes"}:
                    pending = await _pending(session, ctx)
                    if not pending:
                        raise GameError("Действие уже выполнено, отменено или истекло.")
                    args = list(pending.payload["args"])
                    spec = ACTIONS[pending.action]
                    if payload.operation == "v":
                        if len(args) >= len(spec.fields):
                            raise GameError("Выбор уже завершён.")
                        kind = spec.fields[len(args)].kind
                        if kind not in {
                            "resource",
                            "asset",
                            "building",
                            "unit",
                            "item",
                        } or payload.argument not in catalog(kind, rules):
                            raise GameError("Неизвестный вариант.")
                        args.append(payload.argument)
                        pending.payload = {**pending.payload, "args": args}
                    elif len(args) != len(spec.fields):
                        raise GameError("Сначала заполните значения.")
                    text, rows, notice = await _finish_or_prompt(
                        session, rules, ctx, pending, confirm=payload.operation == "yes"
                    )
                else:
                    raise GameError("Неизвестная кнопка.")
            except GameError as exc:
                notice = str(exc)
                text, rows = await render_page(session, rules, ctx.telegram_id, "home")
        await query.answer()
        await _publish(bot, db, rules, context_data(ctx), text, rows, notice)

    @router.message(F.text)
    async def handle_text(message: Message, bot: Bot):
        if not message.from_user or (message.text or "").startswith("/"):
            return
        async with db.sessions() as session, session.begin():
            pending = await session.get(PendingInput, message.from_user.id, with_for_update=True)
            if not pending or pending.chat_id != message.chat.id:
                return
            if aware(pending.expires_at) <= utcnow():
                await session.delete(pending)
                return
            spec = ACTIONS.get(pending.action)
            if not spec:
                await session.delete(pending)
                return
            args = list(pending.payload["args"])
            if len(args) >= len(spec.fields):
                return
            ctx = MenuContext(message.from_user.id, pending.chat_id, pending.message_id, pending.payload["nonce"])
            try:
                field = spec.fields[len(args)]
                if field.kind not in {"int", "id", "name"}:
                    raise GameError("Выберите вариант кнопкой.")
                args.append(parse_input(message.text, field.kind, rules))
                pending.payload = {**pending.payload, "args": args}
                text, rows, notice = await _finish_or_prompt(session, rules, ctx, pending)
            except GameError as exc:
                text, rows = form_view(pending, rules)
                notice = str(exc)
        await _publish(bot, db, rules, context_data(ctx), text, rows, notice)
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    return router
