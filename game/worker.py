"""Separate durable scheduler: python -m game.worker. Multiple workers are safe."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from game.config import RULES, Settings
from game.formatting import configure_log_clock
from game.core import aware
from game.db import Database, SchemaNotReady
from game.models import GameEvent, Job, PendingInput, utcnow
from game.services.alliance import AllianceService
from game.services.country_war import CountryWarService
from game.ticks import disaster_tick, income_tick, production_tick

log = logging.getLogger(__name__)


async def seed_jobs(database: Database, rules: dict, now=None) -> None:
    now = now or utcnow()
    async with database.sessions() as session, session.begin():
        for name in ("production", "income", "disaster", "war"):
            # First income/production occurs after a full interval; no restart windfall.
            delay = rules["jobs"][f"{name}_seconds"] if name in ("production", "income") else 0
            await session.execute(
                insert(Job)
                .values(name=name, next_run=now + timedelta(seconds=delay), runs=0)
                .on_conflict_do_nothing(index_elements=["name"])
            )


async def run_once(database: Database, rules: dict, now=None) -> bool:
    now = now or utcnow()
    async with database.sessions() as session, session.begin():
        job = await session.scalar(
            select(Job)
            .where(Job.next_run <= now)
            .order_by(Job.next_run, Job.name)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return False
        interval = rules["jobs"][f"{job.name}_seconds"]
        due = int((now - aware(job.next_run)).total_seconds() // interval) + 1
        count = min(due, rules["jobs"]["max_catchup_ticks"]) if job.name in ("production", "income") else 1
        # Replay only recent ticks after downtime. A newly created country never receives older ticks.
        first = aware(job.next_run) + timedelta(seconds=(due - count) * interval)
        for index in range(count):
            at = first + timedelta(seconds=index * interval)
            if job.name == "production":
                await production_tick(session, rules, at)
            elif job.name == "income":
                await income_tick(session, rules, at)
            elif job.name == "disaster":
                await disaster_tick(session, rules, now)
            elif job.name == "war":
                await CountryWarService(session, rules).resolve(now=now)
                await AllianceService(session, rules).resolve_due_wars(now=now)
        job.next_run = aware(job.next_run) + timedelta(seconds=due * interval)
        job.runs += count
        if job.name == "disaster":
            await session.execute(
                delete(GameEvent).where(
                    GameEvent.created_at < now - timedelta(days=rules["jobs"]["event_retention_days"])
                )
            )
        log.info("Completed %s: %d tick(s), skipped %d old tick(s)", job.name, count, due - count)
    if job.name == "disaster":
        # UI acquires Panel/Input before country; never delete inputs while holding country locks.
        async with database.sessions() as session, session.begin():
            await session.execute(delete(PendingInput).where(PendingInput.expires_at < now))
    return True


async def main() -> None:
    settings = Settings.from_env()
    configure_log_clock()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        raise SystemExit("Worker requires PostgreSQL with asyncpg; SQLite is only for service unit tests.")
    database = Database(settings.database_url)
    try:
        await database.check_schema()
        await seed_jobs(database, RULES)
        while True:
            try:
                worked = await run_once(database, RULES)
            except Exception:
                # Transaction rollback leaves the job due. A restart never marks failed work as paid.
                log.exception("World update failed; transaction rolled back")
                worked = False
            if not worked:
                await asyncio.sleep(settings.poll_seconds)
    except SchemaNotReady as exc:
        raise SystemExit(str(exc)) from None
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
