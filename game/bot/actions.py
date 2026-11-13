"""Declarative forms keep every game action reachable from the inline panel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    kind: str
    prompt: str


@dataclass(frozen=True)
class Action:
    service: str
    method: str
    title: str
    page: str
    fields: tuple[Field, ...] = ()
    confirm: bool = False


QTY = Field("int", "Введите количество целым положительным числом.")
MONEY = Field("int", "Введите сумму целым положительным числом.")
TARGET = Field("id", "Введите Telegram ID игрока (его можно узнать в панели страны).")
RESOURCE = Field("resource", "Выберите ресурс.")
UNIT = Field("unit", "Выберите военную технику.")
ITEM = Field("item", "Выберите космический артефакт.")

ACTIONS = {
    "create": Action(
        service="game",
        method="create",
        title="Основание страны",
        page="home",
        fields=(Field(kind="name", prompt="Введите уникальное название страны: только латинские буквы без пробелов."),),
        confirm=False,
    ),
    "build": Action(
        service="game",
        method="build",
        title="Строительство",
        page="production",
        fields=(
            Field(kind="building", prompt="Выберите предприятие или дома."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "expand": Action(
        service="game",
        method="expand",
        title="Расширение территории",
        page="production",
        fields=(Field(kind="int", prompt="Введите количество целым положительным числом."),),
        confirm=True,
    ),
    "recruit": Action(
        service="game",
        method="recruit",
        title="Набор военнослужащих",
        page="army",
        fields=(Field(kind="int", prompt="Введите количество целым положительным числом."),),
        confirm=False,
    ),
    "craft": Action(
        service="game",
        method="craft",
        title="Сборка военной техники",
        page="army",
        fields=(
            Field(kind="unit", prompt="Выберите военную технику."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "feed": Action(
        service="game",
        method="feed",
        title="Снабжение армии",
        page="army",
        fields=(Field(kind="int", prompt="Введите количество целым положительным числом."),),
        confirm=False,
    ),
    "indep": Action(service="game", method="independence", title="Независимость", page="home", fields=(), confirm=True),
    "eup": Action(
        service="game", method="energy_upgrade", title="Модернизация энергосети", page="energy", fields=(), confirm=True
    ),
    "efix": Action(
        service="game",
        method="energy_maintain",
        title="Обслуживание энергосети",
        page="energy",
        fields=(),
        confirm=True,
    ),
    "sell": Action(
        service="market",
        method="create_order",
        title="Лот на рынке",
        page="market",
        fields=(
            Field(kind="asset", prompt="Выберите ресурс или технику."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "buy": Action(
        service="market",
        method="buy_order",
        title="Покупка лота по номеру",
        page="market",
        fields=(Field(kind="int", prompt="Введите номер лота."),),
        confirm=True,
    ),
    "cancel": Action(
        service="market",
        method="cancel_order",
        title="Снятие лота",
        page="market",
        fields=(Field(kind="int", prompt="Введите ID своего лота."),),
        confirm=True,
    ),
    "sysbuy": Action(
        service="market",
        method="system_buy",
        title="Импорт у Торговой палаты",
        page="market",
        fields=(
            Field(kind="asset", prompt="Выберите ресурс или технику."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "syssell": Action(
        service="market",
        method="system_sell",
        title="Экспорт Торговой палате",
        page="market",
        fields=(
            Field(kind="asset", prompt="Выберите ресурс или технику."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "send": Action(
        service="market",
        method="transfer",
        title="Денежный перевод",
        page="market",
        fields=(
            Field(kind="id", prompt="Введите Telegram ID игрока (его можно узнать в панели страны)."),
            Field(kind="int", prompt="Введите сумму целым положительным числом."),
        ),
        confirm=True,
    ),
    "spies": Action(
        service="intel",
        method="train_spies",
        title="Подготовка разведчиков",
        page="intel",
        fields=(Field(kind="int", prompt="Введите количество целым положительным числом."),),
        confirm=False,
    ),
    "sup": Action(
        service="intel", method="research_spies", title="Школа разведки", page="intel", fields=(), confirm=True
    ),
    "hup": Action(
        service="intel",
        method="research_hackers",
        title="Киберцентр: открыть / улучшить",
        page="intel",
        fields=(),
        confirm=True,
    ),
    "spy": Action(
        service="intel",
        method="spy",
        title="Поиск цели для кражи",
        page="intel",
        fields=(Field(kind="resource", prompt="Выберите ресурс."),),
        confirm=True,
    ),
    "hack": Action(
        service="intel", method="hack", title="Киберразведка случайной страны", page="intel", fields=(), confirm=True
    ),
    "rocket": Action(
        service="space", method="craft_rocket", title="Постройка ракеты", page="space", fields=(), confirm=True
    ),
    "launch": Action(
        service="space", method="launch", title="Запуск экспедиции", page="space", fields=(), confirm=True
    ),
    "claim": Action(
        service="space", method="claim", title="Возвращение экспедиции", page="space", fields=(), confirm=False
    ),
    "item": Action(
        service="space",
        method="craft_item",
        title="Космическая мастерская",
        page="space",
        fields=(
            Field(kind="item", prompt="Выберите космический артефакт."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "itemsell": Action(
        service="space",
        method="sell_item",
        title="Продажа артефактов",
        page="space",
        fields=(
            Field(kind="item", prompt="Выберите космический артефакт."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=False,
    ),
    "acreate": Action(
        service="alliance",
        method="create",
        title="Создание союза",
        page="alliance",
        fields=(Field(kind="name", prompt="Введите название союза: латинские буквы, цифры или _, первая — буква."),),
        confirm=True,
    ),
    "ajoin": Action(
        service="alliance",
        method="join",
        title="Вступление в союз",
        page="alliance",
        fields=(Field(kind="int", prompt="Введите ID союза из рейтинга."),),
        confirm=True,
    ),
    "aleave": Action(
        service="alliance",
        method="leave",
        title="Выход из союза — переданная техника будет уничтожена",
        page="alliance",
        fields=(),
        confirm=True,
    ),
    "alead": Action(
        service="alliance",
        method="transfer_leadership",
        title="Передача руководства",
        page="alliance",
        fields=(Field(kind="id", prompt="Введите Telegram ID игрока (его можно узнать в панели страны)."),),
        confirm=True,
    ),
    "aunit": Action(
        service="alliance",
        method="contribute_units",
        title="Передача техники союзу",
        page="alliance",
        fields=(
            Field(kind="unit", prompt="Выберите военную технику."),
            Field(kind="int", prompt="Введите количество целым положительным числом."),
        ),
        confirm=True,
    ),
    "apay": Action(
        service="alliance",
        method="payout",
        title="Выплата участнику",
        page="alliance",
        fields=(
            Field(kind="id", prompt="Введите Telegram ID игрока (его можно узнать в панели страны)."),
            Field(kind="int", prompt="Введите сумму целым положительным числом."),
        ),
        confirm=True,
    ),
    "war": Action(
        service="alliance", method="start_war", title="Начало войны союза", page="alliance", fields=(), confirm=True
    ),
    "civilfood": Action(
        service="game",
        method="feed_population",
        title="Выделить еду населению",
        page="population",
        fields=(Field(kind="int", prompt="Введите количество целым положительным числом."),),
        confirm=False,
    ),
    "cwar": Action(
        service="countrywar", method="start", title="Начать войну страны", page="army", fields=(), confirm=True
    ),
}

ACTIONS.pop("ajoin", None)
ACTIONS["ainvite"] = Action("alliance", "invite", "Пригласить в союз", "alliance", (TARGET,), True)
