"""Set-based PostgreSQL world updates: no ORM loop over player countries."""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, DateTime, Float, String, bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from game.models import Disaster, MOSCOW
from game.core import aware


async def execute_sql(session: AsyncSession, sql: str, parameters: dict):
    """Type every bind: PG can otherwise infer fractional damage as bigint or CASE values as text."""
    names = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z_0-9]*)", sql))
    bindings = []
    for name in names:
        value = parameters[name]
        if isinstance(value, datetime):
            datatype = DateTime(timezone=True)
        elif isinstance(value, int):
            datatype = BigInteger()
        elif isinstance(value, float):
            datatype = Float()
        else:
            datatype = String()
        bindings.append(bindparam(name, type_=datatype))
    return await session.execute(text(sql).bindparams(*bindings), parameters)


async def lock_countries(session: AsyncSession, at: datetime) -> None:
    # Same total order as trades/espionage; assets always lock after their owner.
    await execute_sql(
        session,
        """
        SELECT count(*) FROM (
          SELECT id FROM countries WHERE created_at <= :at ORDER BY id FOR NO KEY UPDATE
        ) AS locked
    """,
        {"at": at},
    )


async def production_tick(session: AsyncSession, rules: dict, at: datetime) -> None:
    await lock_countries(session, at)
    catalog = [
        {"kind": key, "resource": spec["resource"], "output": spec["output"]}
        for key, spec in rules["buildings"].items()
        if spec.get("resource")
    ]
    await execute_sql(
        session,
        """
        INSERT INTO inventory (country_id, resource, amount)
        SELECT b.country_id, p.resource, SUM(b.amount * p.output)::bigint
        FROM buildings b
        JOIN jsonb_to_recordset(CAST(:catalog AS jsonb)) AS p(kind text, resource text, output bigint)
          ON p.kind = b.kind
        JOIN countries c ON c.id = b.country_id AND c.created_at <= :at
        WHERE b.amount > 0
        GROUP BY b.country_id, p.resource
        ON CONFLICT (country_id, resource)
        DO UPDATE SET amount = inventory.amount + EXCLUDED.amount
    """,
        {"catalog": json.dumps(catalog), "at": at},
    )
    cfg = dict(rules["economy"], at=at)
    # Calculate satiety before consumption. Partial rations soften hunger decline.
    await execute_sql(
        session,
        """
        WITH rations AS (
            SELECT c.id,
              LEAST(1.0, c.civilian_food::numeric / GREATEST(1, c.population * :food_per_civilian),
                         COALESCE(w.amount, 0)::numeric / GREATEST(1, c.population * :water_per_civilian)) AS civilian,
              LEAST(1.0, c.army_food::numeric / GREATEST(1, (c.soldiers + c.spies) * :food_per_soldier)) AS army
            FROM countries c
            LEFT JOIN inventory f ON f.country_id = c.id AND f.resource = 'food'
            LEFT JOIN inventory w ON w.country_id = c.id AND w.resource = 'water'
            WHERE c.created_at <= :at
        )
        UPDATE countries c SET
          hunger = GREATEST(0, LEAST(100, c.hunger + CASE WHEN r.civilian >= 1 THEN :hunger_recovery ELSE (0.0 - CAST(:hunger_decay AS double precision)) * (1-r.civilian) END)),
          army_hunger = CASE WHEN c.soldiers + c.spies = 0 THEN 100 ELSE
            GREATEST(0, LEAST(100, c.army_hunger + CASE WHEN r.army >= 1 THEN :hunger_recovery ELSE (0.0 - CAST(:hunger_decay AS double precision)) * (1-r.army) END)) END,
          civilian_food = GREATEST(0, c.civilian_food - c.population * :food_per_civilian),
          army_food = GREATEST(0, c.army_food - (c.soldiers+c.spies) * :food_per_soldier)
        FROM rations r WHERE c.id = r.id
    """,
        cfg,
    )
    await execute_sql(
        session,
        """
        UPDATE inventory i SET amount = GREATEST(0, i.amount - c.population *
          :water_per_civilian)
        FROM countries c WHERE i.country_id = c.id AND c.created_at <= :at AND i.resource = 'water'
    """,
        cfg,
    )
    await execute_sql(
        session,
        """
        WITH infrastructure AS (
          SELECT c.id,
            COALESCE(SUM(b.amount) FILTER (WHERE b.kind = 'house'), 0) AS houses,
            COALESCE(SUM(b.amount) FILTER (WHERE b.kind NOT IN ('house','solar','nuclear')), 0) AS factories
          FROM countries c LEFT JOIN buildings b ON b.country_id = c.id
          WHERE c.created_at <= :at GROUP BY c.id
        )
        UPDATE countries c SET
          population = CASE WHEN c.hunger <= :starvation_threshold THEN
            GREATEST(0, c.population - GREATEST(1, FLOOR(c.population * :starvation_rate)::bigint))
          WHEN c.hunger >= :births_hunger_min THEN c.population + GREATEST(0, LEAST(
            i.houses * :house_capacity - c.population, :births_max,
            FLOOR(:births_base + i.houses * :births_per_house + i.factories * :births_per_factory)::bigint))
          ELSE c.population END,
          soldiers = CASE WHEN c.army_hunger <= :starvation_threshold THEN
            GREATEST(0, c.soldiers - GREATEST(1, FLOOR(c.soldiers * :starvation_rate)::bigint)) ELSE c.soldiers END,
          spies = CASE WHEN c.army_hunger <= :starvation_threshold THEN
            GREATEST(0, c.spies - GREATEST(1, FLOOR(c.spies * :starvation_rate)::integer)) ELSE c.spies END
        FROM infrastructure i WHERE c.id = i.id
    """,
        cfg,
    )


async def income_tick(session: AsyncSession, rules: dict, at: datetime) -> None:
    await lock_countries(session, at)
    params = dict(rules["energy"], tax=rules["economy"]["tax_per_person"], at=at)
    params["solar_income"] = rules["energy"]["solar_kwh"] * rules["energy"]["price_per_kwh"]
    params["nuclear_income"] = rules["energy"]["nuclear_kwh"] * rules["energy"]["price_per_kwh"]
    await execute_sql(
        session,
        """
      WITH capacity AS (
        SELECT c.id, COALESCE(s.amount, 0) AS solar,
          CASE WHEN c.energy_condition > 0 THEN LEAST(COALESCE(n.amount, 0), COALESCE(u.amount, 0) / :uranium_per_nuclear) ELSE 0 END AS powered,
          COALESCE(s.amount, 0) + COALESCE(n.amount, 0) AS stations
        FROM countries c
        LEFT JOIN buildings s ON s.country_id = c.id AND s.kind = 'solar'
        LEFT JOIN buildings n ON n.country_id = c.id AND n.kind = 'nuclear'
        LEFT JOIN inventory u ON u.country_id = c.id AND u.resource = 'uranium'
        WHERE c.created_at <= :at
      ), credited AS (
        UPDATE countries c SET money = c.money + c.population * :tax + CASE WHEN c.independent THEN
          FLOOR((p.solar * :solar_income + p.powered * :nuclear_income) *
            (1 + (c.energy_level - 1) * :level_bonus) * c.energy_condition / :condition_max)::bigint ELSE 0 END,
          energy_condition = CASE WHEN c.independent AND p.stations > 0 THEN
            GREATEST(:condition_min, c.energy_condition - :wear_per_hour) ELSE c.energy_condition END
        FROM capacity p WHERE c.id = p.id
        RETURNING c.id, CASE WHEN c.independent THEN p.powered ELSE 0 END AS powered
      )
      UPDATE inventory i SET amount = i.amount - x.powered * :uranium_per_nuclear
      FROM credited x WHERE i.country_id = x.id AND i.resource = 'uranium'
    """,
        params,
    )


async def disaster_tick(session: AsyncSession, rules: dict, at: datetime, rng=None) -> None:
    rng = rng or random.SystemRandom()
    cfg = rules["disasters"]
    local = at.astimezone(ZoneInfo(cfg["timezone"]))
    midnight = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=local.tzinfo)
    seconds = int((next_midnight.timestamp() - midnight.timestamp()))
    await session.execute(
        insert(Disaster)
        .values(
            day=local.date().isoformat(),
            scheduled_at=(midnight.astimezone(timezone.utc) + timedelta(seconds=rng.randrange(seconds))).astimezone(
                MOSCOW
            ),
            kind=rng.choice(cfg["kinds"]),
            damage=rng.uniform(cfg["min_damage"], cfg["max_damage"]),
        )
        .on_conflict_do_nothing(index_elements=["day"])
    )
    due = (
        await session.scalars(
            select(Disaster)
            .where(Disaster.applied_at.is_(None), Disaster.scheduled_at <= at)
            .order_by(Disaster.day)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not due:
        return
    await lock_countries(session, at)
    for event in due:  # Bounded daily events; each statement updates all countries in SQL.
        await apply_disaster(session, rules, event, at)


async def apply_disaster(session: AsyncSession, rules: dict, event: Disaster, at: datetime) -> None:
    if event.applied_at is not None:
        return
    params = {
        "damage": event.damage,
        "at": min(at, aware(event.scheduled_at)),
        "capacity": rules["economy"]["house_capacity"],
    }
    eligible = "c.independent AND c.created_at <= :at"
    if event.kind == "virus":
        await execute_sql(
            session,
            f"UPDATE countries c SET population = population - FLOOR(population * :damage)::bigint WHERE {eligible}",
            params,
        )
    elif event.kind == "meteor":
        await execute_sql(
            session,
            f"""UPDATE buildings b SET amount = b.amount - FLOOR(b.amount * :damage)::integer
          FROM countries c WHERE b.country_id = c.id AND b.kind <> 'house' AND {eligible}""",
            params,
        )
    elif event.kind == "aliens":
        await execute_sql(
            session,
            f"""UPDATE units u SET amount = u.amount - FLOOR(u.amount * :damage)::bigint
          FROM countries c WHERE u.country_id = c.id AND {eligible}""",
            params,
        )
        await execute_sql(
            session,
            f"""UPDATE countries c SET soldiers = soldiers - FLOOR(soldiers * :damage)::bigint,
          spies = spies - FLOOR(spies * :damage)::integer WHERE {eligible}""",
            params,
        )
    elif event.kind == "fire":
        await execute_sql(
            session,
            f"""UPDATE inventory i SET amount = i.amount - FLOOR(i.amount * :damage)::bigint
          FROM countries c WHERE i.country_id = c.id AND {eligible}""",
            params,
        )
    elif event.kind == "earthquake":
        # Read the pre-damage housing count for both losses in the same statement.
        await execute_sql(
            session,
            f"""
          WITH losses AS MATERIALIZED (
            SELECT c.id, FLOOR(b.amount * :damage)::integer AS lost FROM countries c
            JOIN buildings b ON b.country_id = c.id AND b.kind = 'house' WHERE {eligible}
          ), people AS (
            UPDATE countries c SET population = GREATEST(0, c.population - l.lost * :capacity)
            FROM losses l WHERE c.id = l.id RETURNING c.id
          )
          UPDATE buildings b SET amount = b.amount - l.lost FROM losses l
          JOIN people p ON p.id = l.id WHERE b.country_id = l.id AND b.kind = 'house'
        """,
            params,
        )
    else:
        raise ValueError(f"Unknown disaster: {event.kind}")
    names = {
        "virus": "Эпидемия",
        "meteor": "Метеоритный дождь",
        "aliens": "Атака пришельцев",
        "fire": "Пожар склада",
        "earthquake": "Землетрясение",
    }
    params["message"] = f"{names[event.kind]}: ущерб до {event.damage:.1%}. Дробные потери округлены вниз."
    await execute_sql(
        session,
        f"""INSERT INTO game_events (country_id, text, created_at)
      SELECT c.id, :message, :at FROM countries c WHERE {eligible}""",
        params,
    )
    event.applied_at = at
