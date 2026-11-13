"""Persistent domain state; integers represent indivisible currency/resources."""

from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.types import TypeDecorator
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


MOSCOW = ZoneInfo("Europe/Moscow")


class MoscowDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=MOSCOW) if value.tzinfo is None else value.astimezone(MOSCOW)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=MOSCOW) if value.tzinfo is None else value.astimezone(MOSCOW)


def utcnow() -> datetime:
    """Compatibility name; the entire game clock uses Moscow time."""
    return datetime.now(MOSCOW)


class Base(DeclarativeBase):
    pass


class Country(Base):
    __tablename__ = "countries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(24))
    name_key: Mapped[str] = mapped_column(String(24), unique=True)
    money: Mapped[int] = mapped_column(BigInteger, default=0)
    population: Mapped[int] = mapped_column(BigInteger, default=0)
    land: Mapped[int] = mapped_column(Integer, default=0)
    hunger: Mapped[float] = mapped_column(Float, default=100)
    army_hunger: Mapped[float] = mapped_column(Float, default=100)
    soldiers: Mapped[int] = mapped_column(BigInteger, default=0)
    civilian_food: Mapped[int] = mapped_column(BigInteger, default=0)
    war_ready_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    shield_until: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    army_food: Mapped[int] = mapped_column(BigInteger, default=0)
    spies: Mapped[int] = mapped_column(Integer, default=0)
    spy_level: Mapped[int] = mapped_column(Integer, default=1)
    hacker_level: Mapped[int] = mapped_column(Integer, default=0)
    independent: Mapped[bool] = mapped_column(Boolean, default=False)
    spy_ready_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    hack_ready_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    space_ready_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    energy_level: Mapped[int] = mapped_column(Integer, default=1)
    energy_condition: Mapped[float] = mapped_column(Float, default=100)
    created_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    __table_args__ = (
        CheckConstraint(
            "money >= 0 AND population >= 0 AND soldiers >= 0 AND spies >= 0 AND army_food >= 0 AND land >= 0",
            name="country_nonnegative",
        ),
        CheckConstraint(
            "hunger BETWEEN 0 AND 100 AND army_hunger BETWEEN 0 AND 100 AND energy_condition BETWEEN 0 AND 100",
            name="country_condition",
        ),
    )


class Inventory(Base):
    __tablename__ = "inventory"
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id", ondelete="CASCADE"), primary_key=True)
    resource: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("amount >= 0", name="inventory_nonnegative"),)


class Building(Base):
    __tablename__ = "buildings"
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("amount >= 0", name="building_nonnegative"),)


class Unit(Base):
    __tablename__ = "units"
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("amount >= 0", name="unit_nonnegative"),)


class Alliance(Base):
    __tablename__ = "alliances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    name_key: Mapped[str] = mapped_column(String(32), unique=True)
    leader_country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    money: Mapped[int] = mapped_column(BigInteger, default=0)
    disbanded: Mapped[bool] = mapped_column(Boolean, default=False)
    war_ready_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    created_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    __table_args__ = (CheckConstraint("money >= 0", name="alliance_money_nonnegative"),)


class AllianceMember(Base):
    __tablename__ = "alliance_members"
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), primary_key=True)
    alliance_id: Mapped[int] = mapped_column(ForeignKey("alliances.id", ondelete="CASCADE"), index=True)
    money_contributed: Mapped[int] = mapped_column(BigInteger, default=0)
    contribution_credit: Mapped[int] = mapped_column(BigInteger, default=0)
    resource_contributions: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("money_contributed >= 0 AND contribution_credit >= 0", name="member_contributions_nonnegative"),
    )


class AllianceInventory(Base):
    __tablename__ = "alliance_inventory"
    alliance_id: Mapped[int] = mapped_column(ForeignKey("alliances.id", ondelete="CASCADE"), primary_key=True)
    resource: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("amount >= 0", name="alliance_inventory_nonnegative"),)


class AllianceUnit(Base):
    __tablename__ = "alliance_units"
    alliance_id: Mapped[int] = mapped_column(ForeignKey("alliances.id", ondelete="CASCADE"), primary_key=True)
    contributor_country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("amount >= 0", name="alliance_unit_nonnegative"),)


class War(Base):
    __tablename__ = "wars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attacker_id: Mapped[int] = mapped_column(ForeignKey("alliances.id"), index=True)
    defender_id: Mapped[int | None] = mapped_column(ForeignKey("alliances.id"), index=True)
    opponent_name: Mapped[str] = mapped_column(String(64))
    attacker_power: Mapped[int] = mapped_column(BigInteger)
    defender_power: Mapped[int] = mapped_column(BigInteger)
    stake: Mapped[int] = mapped_column(BigInteger, default=0)
    started_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    ends_at: Mapped[datetime] = mapped_column(MoscowDateTime(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("alliances.id"))
    reward: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    report: Mapped[str] = mapped_column(Text, default="")


class MarketOrder(Base):
    __tablename__ = "market_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int | None] = mapped_column(ForeignKey("countries.id"), index=True)
    alliance_id: Mapped[int | None] = mapped_column(ForeignKey("alliances.id"), index=True)
    resource: Mapped[str] = mapped_column(String(32), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    __table_args__ = (
        CheckConstraint("amount >= 0 AND price > 0", name="market_nonnegative"),
        CheckConstraint("(country_id IS NULL) != (alliance_id IS NULL)", name="market_one_owner"),
    )


class TransferLog(Base):
    __tablename__ = "transfer_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow, index=True)


class Expedition(Base):
    __tablename__ = "expeditions"
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), primary_key=True)
    launched_at: Mapped[datetime] = mapped_column(MoscowDateTime())
    returns_at: Mapped[datetime] = mapped_column(MoscowDateTime())
    destroyed: Mapped[bool] = mapped_column(Boolean)


class Job(Base):
    __tablename__ = "jobs"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_run: Mapped[datetime] = mapped_column(MoscowDateTime(), index=True)
    runs: Mapped[int] = mapped_column(BigInteger, default=0)


class Disaster(Base):
    __tablename__ = "disasters"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    scheduled_at: Mapped[datetime] = mapped_column(MoscowDateTime(), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    damage: Mapped[float] = mapped_column(Float)
    applied_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())


class GameEvent(Base):
    __tablename__ = "game_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int | None] = mapped_column(ForeignKey("countries.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow, index=True)


class PendingInput(Base):
    __tablename__ = "pending_inputs"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(MoscowDateTime())
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)


# Avoid scanning closed market offers and resolved wars as game history grows.
Index("ix_market_orders_active_id", MarketOrder.id, postgresql_where=MarketOrder.amount > 0)
Index("ix_wars_due", War.ends_at, postgresql_where=War.resolved_at.is_(None))
Index("ix_game_events_country_recent", GameEvent.country_id, GameEvent.id)
Index("ix_transfer_sender_window", TransferLog.sender_id, TransferLog.created_at)
Index("ix_transfer_recipient_window", TransferLog.recipient_id, TransferLog.created_at)


class CountryWar(Base):
    started_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    __tablename__ = "country_wars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attacker_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), index=True)
    defender_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), index=True)
    attacker_power: Mapped[int] = mapped_column(BigInteger)
    defender_power: Mapped[int] = mapped_column(BigInteger)
    ends_at: Mapped[datetime] = mapped_column(MoscowDateTime(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("countries.id"))
    report: Mapped[str] = mapped_column(Text, default="")


class AllianceInvitation(Base):
    __tablename__ = "alliance_invitations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alliance_id: Mapped[int] = mapped_column(ForeignKey("alliances.id", ondelete="CASCADE"), index=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), index=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    expires_at: Mapped[datetime] = mapped_column(MoscowDateTime())
    answered_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(MoscowDateTime())
    next_attempt: Mapped[datetime] = mapped_column(MoscowDateTime(), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


Index("ix_notifications_pending", Notification.next_attempt, postgresql_where=Notification.sent_at.is_(None))
