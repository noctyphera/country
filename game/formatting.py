"""Shared formatting for domain notifications and Telegram views."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from game.core import aware
from game.config import RULES


def number(value):
    if isinstance(value, (float, Decimal)):
        return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{value:,}".replace(",", ".") if isinstance(value, int) else str(value)


def money(value):
    return "¤" + number(value)


def date_time(value: datetime | None):
    return (
        aware(value).astimezone(ZoneInfo(RULES["display"]["timezone"])).strftime("%d.%m.%Y в %H:%M")
        if value
        else "доступно"
    )


def message_numbers(text: str) -> str:
    """Group bare quantities while preserving dates, times, codes and numbered IDs."""
    import re

    protected = re.split(r"(<code>.*?</code>|\b\d{2}\.\d{2}\.\d{4}\b|\b\d{2}:\d{2}\b)", text, flags=re.S)

    def segment(value):
        def replace(match):
            if re.search(r"(?:№|#|\bID)\s*$", value[: match.start()]):
                return match.group()
            return number(int(match.group()))

        return re.sub(r"(?<![\w.])\d{4,}(?![\w.])", replace, value)

    return "".join(part if index % 2 else segment(part) for index, part in enumerate(protected))


def remaining(value, now=None):
    from game.models import utcnow
    import math

    if not value:
        return "доступно"
    seconds = max(0, math.ceil((aware(value) - (now or utcnow())).total_seconds()))
    if not seconds:
        return "доступно"
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"через {hours} ч. {minutes} мин." if hours else f"через {minutes} мин. {seconds} сек."


def configure_log_clock():
    import logging
    from game.models import MOSCOW

    logging.Formatter.converter = staticmethod(lambda timestamp: datetime.fromtimestamp(timestamp, MOSCOW).timetuple())
