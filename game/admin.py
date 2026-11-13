"""Admin web panel for the game database (SQLAdmin + Starlette).

Run:
    export DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/dbname"   # or postgresql://...
    export ADMIN_USERNAME="admin"
    export ADMIN_PASSWORD="choose-a-strong-password"
    export ADMIN_SECRET_KEY="any-long-random-string"
    uvicorn admin:app --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000/admin

uvicorn admin:app --host 127.0.0.1 --port 8000
"""

import os
import secrets
import sys

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import relationship
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqladmin.filters import BooleanFilter, OperationColumnFilter
from starlette.applications import Starlette
from pathlib import Path
from dotenv import load_dotenv

from models import (
    Alliance,
    AllianceInventory,
    AllianceMember,
    AllianceUnit,
    Building,
    Country,
    CountryWar,
    Disaster,
    Expedition,
    GameEvent,
    Inventory,
    Job,
    MarketOrder,
    Panel,
    PendingInput,
    TransferLog,
    Unit,
    War,
)

# ---------------------------------------------------------------- config
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_USERNAME = os.environ.get("POSTGRES_USER", "admin")
ADMIN_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "")

if not (DATABASE_URL and ADMIN_PASSWORD and SECRET_KEY):
    sys.exit("Set DATABASE_URL, ADMIN_PASSWORD and ADMIN_SECRET_KEY environment variables.")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Works with both async (asyncpg) and sync (psycopg2) URLs.
if "+asyncpg" in DATABASE_URL:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# --------------------------------------------------- friendlier foreign keys
# SQLAdmin edits foreign keys through relationships; the models only define the
# raw *_id columns, so we attach simple relationships here (no schema change).
# War, CountryWar and TransferLog have several FKs to the same table, so those
# ids stay as raw numbers.
Country.__str__ = lambda self: f"{self.name} (#{self.id})"
Alliance.__str__ = lambda self: f"{self.name} (#{self.id})"

Alliance.leader_country = relationship(Country)
AllianceMember.country = relationship(Country)
AllianceMember.alliance = relationship(Alliance)
Inventory.country = relationship(Country)
Building.country = relationship(Country)
Unit.country = relationship(Country)
AllianceInventory.alliance = relationship(Alliance)
AllianceUnit.alliance = relationship(Alliance, foreign_keys=[AllianceUnit.alliance_id])
AllianceUnit.contributor = relationship(Country, foreign_keys=[AllianceUnit.contributor_country_id])
Expedition.country = relationship(Country)
GameEvent.country = relationship(Country)
MarketOrder.country = relationship(Country)
MarketOrder.alliance = relationship(Alliance)


# ------------------------------------------------------------------ auth
class AdminAuth(AuthenticationBackend):
    async def login(self, request) -> bool:
        form = await request.form()
        user_ok = secrets.compare_digest(str(form.get("username", "")), ADMIN_USERNAME)
        pass_ok = secrets.compare_digest(str(form.get("password", "")), ADMIN_PASSWORD)
        if user_ok and pass_ok:
            request.session.update({"admin": True})
            return True
        return False

    async def logout(self, request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request) -> bool:
        return bool(request.session.get("admin"))


# ----------------------------------------------------------------- views
class Base(ModelView):
    page_size = 50
    page_size_options = [25, 50, 100, 500]
    can_export = True
    export_types = ["csv", "json"]
    can_view_details = True


def num(col):
    return OperationColumnFilter(col)


class CountryAdmin(Base, model=Country):
    name = "Country"
    name_plural = "Countries"
    icon = "fa-solid fa-flag"
    column_list = [
        Country.id,
        Country.name,
        Country.telegram_id,
        Country.money,
        Country.population,
        Country.land,
        Country.soldiers,
        Country.spies,
        Country.civilian_food,
        Country.army_food,
        Country.hunger,
        Country.army_hunger,
        Country.independent,
        Country.shield_until,
        Country.war_ready_at,
        Country.created_at,
    ]
    column_searchable_list = [Country.name, Country.telegram_id]
    column_sortable_list = [
        Country.id,
        Country.name,
        Country.money,
        Country.population,
        Country.land,
        Country.soldiers,
        Country.spies,
        Country.civilian_food,
        Country.army_food,
        Country.hunger,
        Country.army_hunger,
        Country.energy_level,
        Country.shield_until,
        Country.war_ready_at,
        Country.created_at,
    ]
    column_default_sort = [(Country.id, False)]
    column_filters = [
        BooleanFilter(Country.independent),
        num(Country.money),
        num(Country.population),
        num(Country.land),
        num(Country.soldiers),
        num(Country.spies),
        num(Country.civilian_food),
        num(Country.army_food),
        num(Country.hunger),
        num(Country.army_hunger),
        num(Country.shield_until),
        num(Country.war_ready_at),
        num(Country.created_at),
    ]


class InventoryAdmin(Base, model=Inventory):
    icon = "fa-solid fa-box"
    column_list = [Inventory.country, Inventory.resource, Inventory.amount]
    column_searchable_list = [Inventory.resource]
    column_sortable_list = [Inventory.resource, Inventory.amount]
    column_filters = [num(Inventory.amount), num(Inventory.resource)]


class BuildingAdmin(Base, model=Building):
    icon = "fa-solid fa-city"
    column_list = [Building.country, Building.kind, Building.amount]
    column_searchable_list = [Building.kind]
    column_sortable_list = [Building.kind, Building.amount]
    column_filters = [num(Building.amount), num(Building.kind)]


class UnitAdmin(Base, model=Unit):
    icon = "fa-solid fa-person-rifle"
    column_list = [Unit.country, Unit.kind, Unit.amount]
    column_searchable_list = [Unit.kind]
    column_sortable_list = [Unit.kind, Unit.amount]
    column_filters = [num(Unit.amount), num(Unit.kind)]


class AllianceAdmin(Base, model=Alliance):
    icon = "fa-solid fa-handshake"
    column_list = [
        Alliance.id,
        Alliance.name,
        Alliance.leader_country,
        Alliance.money,
        Alliance.disbanded,
        Alliance.war_ready_at,
        Alliance.created_at,
    ]
    column_searchable_list = [Alliance.name]
    column_sortable_list = [Alliance.id, Alliance.name, Alliance.money, Alliance.created_at]
    column_filters = [BooleanFilter(Alliance.disbanded), num(Alliance.money), num(Alliance.created_at)]


class AllianceMemberAdmin(Base, model=AllianceMember):
    icon = "fa-solid fa-users"
    column_list = [
        AllianceMember.country,
        AllianceMember.alliance,
        AllianceMember.money_contributed,
        AllianceMember.contribution_credit,
    ]
    column_sortable_list = [AllianceMember.money_contributed, AllianceMember.contribution_credit]
    column_filters = [num(AllianceMember.money_contributed), num(AllianceMember.contribution_credit)]


class AllianceInventoryAdmin(Base, model=AllianceInventory):
    icon = "fa-solid fa-warehouse"
    column_list = [AllianceInventory.alliance, AllianceInventory.resource, AllianceInventory.amount]
    column_searchable_list = [AllianceInventory.resource]
    column_sortable_list = [AllianceInventory.resource, AllianceInventory.amount]
    column_filters = [num(AllianceInventory.amount), num(AllianceInventory.resource)]


class AllianceUnitAdmin(Base, model=AllianceUnit):
    icon = "fa-solid fa-shield"
    column_list = [AllianceUnit.alliance, AllianceUnit.contributor, AllianceUnit.kind, AllianceUnit.amount]
    column_searchable_list = [AllianceUnit.kind]
    column_sortable_list = [AllianceUnit.kind, AllianceUnit.amount]
    column_filters = [num(AllianceUnit.amount), num(AllianceUnit.kind)]


class WarAdmin(Base, model=War):
    name = "Alliance war"
    name_plural = "Alliance wars"
    icon = "fa-solid fa-explosion"
    column_list = [
        War.id,
        War.attacker_id,
        War.defender_id,
        War.opponent_name,
        War.attacker_power,
        War.defender_power,
        War.stake,
        War.status,
        War.winner_id,
        War.reward,
        War.started_at,
        War.ends_at,
        War.resolved_at,
    ]
    column_searchable_list = [War.opponent_name, War.status]
    column_sortable_list = [War.id, War.stake, War.reward, War.status, War.started_at, War.ends_at]
    column_default_sort = [(War.id, True)]
    column_filters = [num(War.status), num(War.stake), num(War.reward), num(War.ends_at), num(War.started_at)]


class CountryWarAdmin(Base, model=CountryWar):
    name = "Country war"
    name_plural = "Country wars"
    icon = "fa-solid fa-crosshairs"
    column_list = [
        CountryWar.id,
        CountryWar.attacker_id,
        CountryWar.defender_id,
        CountryWar.attacker_power,
        CountryWar.defender_power,
        CountryWar.winner_id,
        CountryWar.ends_at,
        CountryWar.resolved_at,
    ]
    column_sortable_list = [
        CountryWar.id,
        CountryWar.attacker_power,
        CountryWar.defender_power,
        CountryWar.ends_at,
        CountryWar.resolved_at,
    ]
    column_default_sort = [(CountryWar.id, True)]
    column_filters = [
        num(CountryWar.attacker_id),
        num(CountryWar.defender_id),
        num(CountryWar.winner_id),
        num(CountryWar.attacker_power),
        num(CountryWar.defender_power),
        num(CountryWar.ends_at),
    ]


class MarketOrderAdmin(Base, model=MarketOrder):
    icon = "fa-solid fa-store"
    column_list = [
        MarketOrder.id,
        MarketOrder.country,
        MarketOrder.alliance,
        MarketOrder.resource,
        MarketOrder.amount,
        MarketOrder.price,
        MarketOrder.created_at,
    ]
    column_searchable_list = [MarketOrder.resource]
    column_sortable_list = [
        MarketOrder.id,
        MarketOrder.resource,
        MarketOrder.amount,
        MarketOrder.price,
        MarketOrder.created_at,
    ]
    column_default_sort = [(MarketOrder.id, True)]
    column_filters = [num(MarketOrder.resource), num(MarketOrder.amount), num(MarketOrder.price)]


class TransferLogAdmin(Base, model=TransferLog):
    icon = "fa-solid fa-money-bill-transfer"
    column_list = [
        TransferLog.id,
        TransferLog.sender_id,
        TransferLog.recipient_id,
        TransferLog.amount,
        TransferLog.created_at,
    ]
    column_sortable_list = [TransferLog.id, TransferLog.amount, TransferLog.created_at]
    column_default_sort = [(TransferLog.id, True)]
    column_filters = [
        num(TransferLog.sender_id),
        num(TransferLog.recipient_id),
        num(TransferLog.amount),
        num(TransferLog.created_at),
    ]


class ExpeditionAdmin(Base, model=Expedition):
    icon = "fa-solid fa-rocket"
    column_list = [Expedition.country, Expedition.launched_at, Expedition.returns_at, Expedition.destroyed]
    column_sortable_list = [Expedition.launched_at, Expedition.returns_at]
    column_filters = [BooleanFilter(Expedition.destroyed), num(Expedition.returns_at)]


class JobAdmin(Base, model=Job):
    icon = "fa-solid fa-clock"
    column_list = [Job.name, Job.next_run, Job.runs]
    column_sortable_list = [Job.name, Job.next_run, Job.runs]
    column_filters = [num(Job.next_run), num(Job.runs)]


class DisasterAdmin(Base, model=Disaster):
    icon = "fa-solid fa-hurricane"
    column_list = [Disaster.day, Disaster.kind, Disaster.damage, Disaster.scheduled_at, Disaster.applied_at]
    column_searchable_list = [Disaster.kind, Disaster.day]
    column_sortable_list = [Disaster.day, Disaster.kind, Disaster.damage, Disaster.scheduled_at]
    column_default_sort = [(Disaster.scheduled_at, True)]
    column_filters = [num(Disaster.kind), num(Disaster.damage), num(Disaster.scheduled_at)]


class GameEventAdmin(Base, model=GameEvent):
    icon = "fa-solid fa-scroll"
    column_list = [GameEvent.id, GameEvent.country, GameEvent.text, GameEvent.created_at]
    column_searchable_list = [GameEvent.text]
    column_sortable_list = [GameEvent.id, GameEvent.created_at]
    column_default_sort = [(GameEvent.id, True)]
    column_filters = [num(GameEvent.created_at)]


class PanelAdmin(Base, model=Panel):
    # Composite primary key: (telegram_id, chat_id, message_id)
    icon = "fa-solid fa-window-maximize"
    column_list = [Panel.telegram_id, Panel.chat_id, Panel.message_id, Panel.revision, Panel.page]
    column_searchable_list = [Panel.telegram_id, Panel.page]
    column_sortable_list = [Panel.revision, Panel.page]


class PendingInputAdmin(Base, model=PendingInput):
    icon = "fa-solid fa-keyboard"
    column_list = [PendingInput.telegram_id, PendingInput.action, PendingInput.payload, PendingInput.expires_at]
    column_searchable_list = [PendingInput.telegram_id, PendingInput.action]
    column_sortable_list = [PendingInput.action, PendingInput.expires_at]
    column_filters = [num(PendingInput.expires_at)]


# ------------------------------------------------------------------- app
app = Starlette()
admin = Admin(
    app,
    engine,
    title="Game Admin",
    authentication_backend=AdminAuth(secret_key=SECRET_KEY),
)

for view in (
    CountryAdmin,
    InventoryAdmin,
    BuildingAdmin,
    UnitAdmin,
    AllianceAdmin,
    AllianceMemberAdmin,
    AllianceInventoryAdmin,
    AllianceUnitAdmin,
    WarAdmin,
    CountryWarAdmin,
    MarketOrderAdmin,
    TransferLogAdmin,
    ExpeditionAdmin,
    JobAdmin,
    DisasterAdmin,
    GameEventAdmin,
    PanelAdmin,
    PendingInputAdmin,
):
    admin.add_view(view)
