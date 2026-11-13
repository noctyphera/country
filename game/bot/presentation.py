"""Pure rendering helpers: compact, owner-scoped Telegram callback payloads."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from game.formatting import number, money, date_time  # noqa: F401


@dataclass(frozen=True)
class Callback:
    owner: int
    nonce: int
    operation: str
    argument: str = ""

    def pack(self) -> str:
        data = f"c:{self.owner}:{self.nonce}:{self.operation}:{self.argument}"
        if len(data.encode()) > 64:
            raise ValueError("Callback payload exceeds Telegram's 64-byte limit")
        return data

    @classmethod
    def unpack(cls, data: str) -> "Callback":
        prefix, owner, nonce, operation, argument = data.split(":", 4)
        if prefix != "c" or not owner.isdecimal() or not nonce.isdecimal():
            raise ValueError("Invalid callback payload")
        return cls(int(owner), int(nonce), operation, argument)


def keyboard(owner: int, nonce: int, rows: list[list[tuple[str, str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=Callback(owner, nonce, operation, argument).pack())
                for label, operation, argument in row
            ]
            for row in rows
        ]
    )


def ordered(data, rules):
    order = rules.get("ui_order", list(rules["resources"]) + list(rules["units"]))
    return sorted(data, key=lambda k: (order.index(k) if k in order else len(order), k))


def label(key: str, rules: dict[str, Any]) -> str:
    for section in ("resources", "buildings", "units", "items"):
        spec = rules.get(section, {}).get(key)
        if isinstance(spec, dict):
            return escape(str(spec.get("name", key)))
    item = rules.get("space", {}).get("items", {}).get(key)
    if item:
        return escape(str(item.get("name", key)))
    return escape(key)


def inventory_lines(data: dict[str, Any], rules: dict[str, Any]) -> str:
    return (
        "\n".join(f"• {label(k, rules)}: <b>{number(v)}</b>" for k in ordered(data, rules) for v in [data[k]] if v)
        or "Пока пусто."
    )


def chunks(items: list[Any], size: int = 2) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
