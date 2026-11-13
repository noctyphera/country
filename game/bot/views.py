"""Player-facing views with shared ordering, numbers and local dates."""

from html import escape
import math
from sqlalchemy import select, or_, func
from game.bot.presentation import inventory_lines, label, number, money, date_time, ordered
from game.models import Country, Expedition, GameEvent, CountryWar, utcnow
from game.services.country import GameService
from game.services.alliance import AllianceService
from game.services.market import MarketService
from game.formatting import remaining
from game.core import aware

Row = list[tuple[str, str, str]]


def nav(back="home"):
    return [[("‹ Назад", "p", back), ("⌂ Главная", "p", "home")]]


def action(text, code, value=""):
    return text, "a", code + ("~" + value if value else "")


def format_cost(spec, rules):
    cost = spec.get("cost", {})
    parts = [f"{label(k, rules)} {number(cost[k])}" for k in ordered(cost, rules)]
    if spec.get("money"):
        parts.insert(0, money(spec["money"]))
    return ", ".join(parts) or "без затрат"


def format_stats(data, rules):
    return (
        f"<b>🏛 {escape(data['name'])}</b>\nID президента: <code>{data['telegram_id']}</code>\n"
        f"Казна: {money(data['money'])}\nНаселение: {number(data['population'])}\n"
        f"ВВП на жителя: {number(data['gdp'])}\nНезависимость: {'да' if data['independent'] else 'нет'}"
    )


async def render_page(session, rules, telegram_id, page):
    c = await session.scalar(select(Country).where(Country.telegram_id == telegram_id))
    if c is None:
        return "<b>🌍 Основать страну</b>\nНазвание — уникальные латинские буквы.", [
            [action("Создать страну", "create")]
        ]
    data = await GameService(session, rules).snapshot(telegram_id)
    home = [
        [("🏭 Производство", "p", "production"), ("📦 Склад", "p", "warehouse")],
        [("👥 Население", "p", "population"), ("🪖 Армия", "p", "army")],
        [("📜 События", "p", "events")],
    ]
    if c.independent:
        home.insert(2, [("🤝 Союзы", "p", "alliance"), ("⚡ Энергетика", "p", "energy")])
        home.insert(3, [("🚀 Космос", "p", "space")])
    else:
        home.append([action("🗽 Независимость", "indep")])
    if page in ("energy", "space", "alliance") and not c.independent:
        return "Раздел откроется после получения независимости.", nav()
    if page == "home":
        return format_stats(data, rules), home
    if page == "warehouse":
        return "<b>📦 Склад</b>\n" + inventory_lines(data["resources"], rules), [[("⚖ Рынок", "p", "market")]] + nav()
    if page == "production":
        lines = [
            "<b>🏭 Производство</b>",
            f"Территория: {number(data['used_land'])} / {number(c.land)}",
            f"Цикл: {number(rules['jobs']['production_seconds'] / 60)} мин.",
        ]
        for key in ordered(rules["resources"], rules):
            factories = [(k, v) for k, v in rules["buildings"].items() if v.get("resource") == key]
            for k, v in factories:
                if v.get("independent") and not c.independent:
                    continue
                n = data["buildings"].get(k, 0)
                lines.append(
                    f"{label(key, rules)}: <b>+{number(n * v['output'])}</b> за цикл · {number(n)} × {number(v['output'])}"
                )
        return "\n".join(lines), [[action("Построить", "build"), action("Расширить", "expand")]] + nav()
    if page == "population":
        text = f"<b>👥 Население</b>\nГраждане: {number(c.population)}\nЖильё: {number(data['housing_capacity'])} мест\nСытость: {number(c.hunger)}%\nРезерв еды: {number(c.civilian_food)}\nНалоги за час: {money(data['tax_income'])}\nЕда за цикл: {number(c.population * rules['economy']['food_per_civilian'])}\nВода за цикл: {number(c.population * rules['economy']['water_per_civilian'])}\nЕду выдавайте со склада; вода расходуется автоматически."
        return text, [[action("Выделить еду", "civilfood"), action("Построить дома", "build", "house")]] + nav()
    if page == "army":
        active = await session.scalar(
            select(CountryWar)
            .where(
                CountryWar.resolved_at.is_(None), or_(CountryWar.attacker_id == c.id, CountryWar.defender_id == c.id)
            )
            .order_by(CountryWar.id.desc())
            .limit(1)
        )
        if active:
            start = aware(active.started_at)
            end = aware(active.ends_at)
            fraction = max(0, min(1, (utcnow() - start).total_seconds() / max(1, (end - start).total_seconds())))
            filled = int(fraction * 10)
            bar = "▰" * filled + "▱" * (10 - filled)
            names = dict(
                (
                    await session.execute(
                        select(Country.id, Country.name).where(Country.id.in_([active.attacker_id, active.defender_id]))
                    )
                ).all()
            )
            text = f"<b>⚔ Война</b>\n{escape(names[active.attacker_id])}: {number(active.attacker_power)}\n{escape(names[active.defender_id])}: {number(active.defender_power)}\n{bar} {number(fraction * 100)}%"
            return text, [[("⟳ Обновить", "p", "army")]] + nav()
        total, wins = (
            await session.execute(
                select(func.count(CountryWar.id), func.count(CountryWar.id).filter(CountryWar.winner_id == c.id)).where(
                    CountryWar.resolved_at.is_not(None),
                    or_(CountryWar.attacker_id == c.id, CountryWar.defender_id == c.id),
                )
            )
        ).one()
        text = (
            f"<b>🪖 Армия</b>\nМощь: {number(data['military_power'])}\nПобед: {number(wins)} · Поражений: {number(total - wins)}\nСолдаты: {number(c.soldiers)}\nРазведчики: {number(c.spies)}\nСытость: {number(c.army_hunger)}%\nЗапас еды: {number(c.army_food)}\nРасход за цикл: {number((c.soldiers + c.spies) * rules['economy']['food_per_soldier'])}\n"
            + inventory_lines(data["units"], rules)
        )
        latest = await session.scalar(
            select(CountryWar)
            .where(
                CountryWar.resolved_at.is_not(None), or_(CountryWar.attacker_id == c.id, CountryWar.defender_id == c.id)
            )
            .order_by(CountryWar.id.desc())
            .limit(1)
        )
        if latest:
            text += "\n\n" + escape(latest.report)
        if c.shield_until and aware(c.shield_until) > utcnow():
            text += "\nЩит закончится " + remaining(c.shield_until)
        if c.war_ready_at and aware(c.war_ready_at) > utcnow():
            text += "\nСледующая война " + remaining(c.war_ready_at)
        return text, [
            [action("Мобилизация", "recruit"), action("Собрать технику", "craft")],
            [action("Выделить еду", "feed"), ("🕵 Разведка", "p", "intel")],
            [action("⚔ Война страны", "cwar")],
        ] + nav()
    if page == "intel":
        cfg = rules["intelligence"]
        text = f"<b>🕵 Разведка</b>\nРазведчики: {number(c.spies)}\nУровень: {number(c.spy_level)}\nКиберцентр: {number(c.hacker_level)}\nЦель выбирается случайно по ВВП ±{number(cfg['gdp_tolerance'] * 100)}%.\nКража: раз в {number(cfg['spy_cooldown_hours'])} ч.\nРазведка: раз в {number(cfg['hack_cooldown_hours'])} ч.\nКража доступна: {remaining(c.spy_ready_at)}\nКибероперация: {remaining(c.hack_ready_at)}"
        rows = [
            [action("Обучить разведчиков", "spies"), action("Улучшить разведку", "sup")],
            [action("Кража ресурсов", "spy")],
        ]
        if c.independent:
            rows.append([action("Киберцентр ↑", "hup"), action("Разведсводка", "hack")])
        return text, rows + nav("army")
    if page == "energy":
        cfg = rules["energy"]
        s = data["buildings"].get("solar", 0)
        n = data["buildings"].get("nuclear", 0)
        powered = (
            min(n, data["resources"].get("uranium", 0) // cfg["uranium_per_nuclear"]) if c.energy_condition > 0 else 0
        )
        kwh = (s * cfg["solar_kwh"] + powered * cfg["nuclear_kwh"]) * (1 + (c.energy_level - 1) * cfg["level_bonus"])
        gross = kwh * cfg["price_per_kwh"]
        net = math.floor(gross * c.energy_condition / cfg["condition_max"])
        text = f"<b>⚡ Энергетика</b>\nСолнечных: {number(s)} · АЭС: {number(n)} (топлива хватит на {number(powered)})\nУровень: {number(c.energy_level)}\nСостояние: {number(c.energy_condition)}%\nВыработка: {number(kwh * c.energy_condition / cfg['condition_max'])} кВт⋅ч за цикл\nТариф: {money(cfg['price_per_kwh'])}/кВт⋅ч\nВыручка: {money(net)}\nИзнос уменьшает выручку на {money(math.floor(gross) - net)}\nЦикл: {number(rules['jobs']['income_seconds'] / 60)} мин.\nМодернизация добавляет {number(cfg['level_bonus'] * 100)}% базовой выработки.\nИзнос: −{number(cfg['wear_per_hour'])} п.п./цикл. Ремонт: +{number(cfg['maintenance_restore'])} п.п."
        return text, [
            [action("☀ Солнечная", "build", "solar"), action("☢ АЭС", "build", "nuclear")],
            [action("Ремонт", "efix"), action("Модернизация", "eup")],
        ] + nav()
    if page == "space":
        e = await session.get(Expedition, c.id)
        cfg = rules["space"]
        text = (
            f"<b>🚀 Космос</b>\nПолёт {number(cfg['duration_hours'])} ч. Риск взрыва {number(cfg['explosion_chance'] * 100)}%.\nРакета: "
            + format_cost({"money": cfg["rocket_money"], "cost": cfg["rocket_cost"]}, rules)
        )
        text += (
            "\nВозвращение: " + remaining(e.returns_at) if e else "\nЗапуск доступен: " + remaining(c.space_ready_at)
        )
        return text, [
            [action("Ракета", "rocket"), action("Запустить", "launch")],
            [action("Приземлить", "claim")],
            [action("Создать предмет", "item"), action("Продать предмет", "itemsell")],
        ] + nav()
    if page.startswith("market") or page.startswith("offers"):
        if page.startswith("offers"):
            index = int(page.split(".")[1]) if "." in page else 0
            orders = await MarketService(session, rules).orders(telegram_id, index)
            text = "<b>Мои лоты</b>\n" + (
                "\n".join(
                    f"№{r['id']} · {label(r['resource'], rules)} × {number(r['amount'])} · {money(r['price'] * r['amount'])} всего"
                    for r in orders
                )
                or "Открытых лотов нет."
            )
            rows = []
            if index:
                rows.append([("‹ Предыдущие", "p", f"offers.{index - 1}")])
            if len(orders) == rules["market"]["page_size"]:
                rows.append([("Следующие ›", "p", f"offers.{index + 1}")])
            return text, rows + [[action("Снять лот", "cancel")]] + nav("market")
        return (
            "<b>⚖ Рынок</b>\nЛоты доступны по номеру. После ввода номера проверьте товар и подтвердите покупку.\nМаксимум открытых лотов: "
            + number(rules["market"]["max_open_orders"]),
            [
                [("Мои лоты", "p", "offers"), action("Купить по номеру", "buy")],
                [action("Выставить лот", "sell")],
                [action("Импорт", "sysbuy"), action("Экспорт", "syssell")],
                [action("Перевод денег", "send")],
            ]
            + nav("warehouse"),
        )
    if page in {"alliance", "alliance.members"}:
        svc = AllianceService(session, rules)
        a = await svc.snapshot(telegram_id)
        if not a:
            return "<b>🤝 Союзы</b>\nВступление — только по приглашению главы. Передавайте технику в армию союза.", [
                [action("Создать", "acreate")],
                [("Топ-10 союзов", "p", "rank.alliance")],
            ] + nav()
        if page == "alliance.members":
            text = "<b>Участники и вклад в мощь</b>\n" + "\n".join(
                f"{escape(m['name'])} · ID <code>{m['telegram_id']}</code> · {number(m['power_percent'])}%"
                for m in a["members"]
            )
            return text, nav("alliance")
        text = (
            f"<b>🤝 {escape(a['name'])}</b> · №{a['id']}\nКазна: {money(a['money'])}\nМощь: {number(a['power'])}\nУчастников: {len(a['members'])}/{rules['alliance']['max_members']}\n"
            + inventory_lines(a["units"], rules)
        )
        w = a.get("war")
        if w:
            text += "\n\n<b>⚔ Война</b>\n" + (
                f"Противник: {escape(w['opponent'])}\nСилы: {number(w['attacker_power'])} / {number(w['defender_power'])}\nЗавершится {remaining(w['ends_at'])}"
                if w["status"] == "active"
                else escape(w["report"])
            )
        text += f"\nНаграда победителю: {number(rules['alliance']['reward_multiplier'])}× стоимости техники обеих сторон. Победитель расходует всю технику; проигравший сохраняет {number(rules['alliance']['loser_retention'] * 100)}%."
        rows = [[action("Передать технику", "aunit"), ("Участники и вклады", "p", "alliance.members")]]
        if a["leader_telegram_id"] == telegram_id:
            rows.extend(
                [
                    [action("Пригласить игрока", "ainvite")],
                    [action("Начать войну", "war"), action("Выплатить", "apay")],
                    [action("Передать главу", "alead")],
                ]
            )
        return text, rows + [[action("Покинуть", "aleave"), ("Топ-10 союзов", "p", "rank.alliance")]] + nav()
    if page == "rank":
        return "<b>🏆 Рейтинг</b>\nВыберите показатель.", [
            [("Деньги", "p", "rank.money"), ("ВВП", "p", "rank.gdp")],
            [("Военная мощь", "p", "rank.power"), ("Прошедшие войны", "p", "rank.wars")],
        ] + nav()
    if page.startswith("rank"):
        metric = page.split(".")[1] if "." in page else "money"
        if metric == "rating":
            metric = "money"
        result = (
            await AllianceService(session, rules).rankings()
            if metric == "alliance"
            else await GameService(session, rules).rankings(metric)
        )
        text = "<b>🏆 Рейтинг</b>\n" + "\n".join(
            f"{i}. {escape(r['name'])} · {number(r.get('power' if metric == 'alliance' else 'military_power' if metric == 'power' else metric, 0))}"
            + (f" · №{r['id']}" if metric == "alliance" else "")
            for i, r in enumerate(result, 1)
        )
        return text, nav("alliance" if metric == "alliance" else "rank")
    if page == "events":
        events = await session.scalars(
            select(GameEvent)
            .where(or_(GameEvent.country_id == c.id, GameEvent.country_id.is_(None)))
            .order_by(GameEvent.id.desc())
            .limit(rules["ui"]["events_limit"])
        )
        return "<b>📜 События</b>\n" + "\n\n".join(
            date_time(e.created_at) + "\n" + escape(e.text) for e in events
        ), nav()
    return format_stats(data, rules), home
