"""Startup must reject unmigrated databases before contacting Telegram or paying ticks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import text

from game.config import Settings
from game.db import Database, SchemaNotReady, migration_heads
from game.models import Base


@pytest_asyncio.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    yield db
    await db.close()


async def test_empty_database_reports_migration_command(database):
    with pytest.raises(SchemaNotReady, match="alembic upgrade head") as exc:
        await database.check_schema()
    assert "countries" in str(exc.value)
    assert "не задана" in str(exc.value)


@pytest.mark.parametrize("revision", [None, "0001", "unknown"])
async def test_tables_without_matching_migrations_are_rejected(database, revision):
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if revision is not None:
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            await conn.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision})
    with pytest.raises(SchemaNotReady, match="Версия базы"):
        await database.check_schema()


async def test_current_schema_passes_but_missing_country_table_is_rejected(database, monkeypatch, tmp_path):
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": next(iter(migration_heads()))})
    monkeypatch.chdir(tmp_path)  # Migration lookup must not depend on the launch directory.
    await database.check_schema()
    async with database.engine.begin() as conn:
        await conn.execute(text("DROP TABLE countries"))
    with pytest.raises(SchemaNotReady, match="countries"):
        await database.check_schema()


@pytest.mark.parametrize("entrypoint", ["bot", "worker"])
async def test_schema_check_prevents_startup_and_closes_database(monkeypatch, entrypoint):
    import game.__main__ as bot_main
    import game.worker as worker_main

    module = bot_main if entrypoint == "bot" else worker_main
    db = SimpleNamespace(
        check_schema=AsyncMock(side_effect=SchemaNotReady("Run alembic upgrade head")), close=AsyncMock()
    )
    settings = Settings(bot_token="test-token", database_url="postgresql+asyncpg://example/test")
    monkeypatch.setattr(module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(module, "Database", Mock(return_value=db))
    if entrypoint == "bot":
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), set_my_commands=AsyncMock())
        dispatcher = SimpleNamespace(include_router=Mock(), start_polling=AsyncMock())
        monkeypatch.setattr(module, "Bot", Mock(return_value=bot))
        monkeypatch.setattr(module, "Dispatcher", Mock(return_value=dispatcher))
        monkeypatch.setattr(module, "make_router", Mock())
    else:
        seed = AsyncMock()
        monkeypatch.setattr(module, "seed_jobs", seed)
    with pytest.raises(SystemExit, match="alembic upgrade head"):
        await module.main()
    db.check_schema.assert_awaited_once()
    db.close.assert_awaited_once()
    if entrypoint == "bot":
        bot.set_my_commands.assert_not_awaited()
        dispatcher.start_polling.assert_not_awaited()
        bot.session.close.assert_awaited_once()
    else:
        seed.assert_not_awaited()
