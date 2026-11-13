"""One AsyncSession per transaction. PostgreSQL is the production backend."""

import asyncio
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from game.models import Base

MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent.parent / "migrations"


def migration_heads() -> set[str]:
    return set(ScriptDirectory(str(MIGRATIONS_DIRECTORY)).get_heads())


class SchemaNotReady(RuntimeError):
    """The configured database cannot safely serve this application version."""


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(
            url,
            pool_pre_ping=True,
            **(
                {"connect_args": {"server_settings": {"timezone": "Europe/Moscow"}}}
                if url.startswith("postgresql")
                else {}
            ),
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def check_schema(self) -> None:
        """Check migrations and required tables before polling or world updates."""
        expected_heads = await asyncio.to_thread(migration_heads)

        def check(connection):
            inspector = inspect(connection)
            existing = set(inspector.get_table_names())
            missing = sorted(set(Base.metadata.tables) - existing)
            actual_heads = set(MigrationContext.configure(connection).get_current_heads())
            problems = []
            if missing:
                problems.append("Не найдены таблицы: " + ", ".join(missing))
            if actual_heads != expected_heads:
                problems.append(
                    f"Версия базы: {', '.join(sorted(actual_heads)) or 'не задана'}; "
                    f"требуется: {', '.join(sorted(expected_heads))}"
                )
            if problems:
                raise SchemaNotReady(
                    "База данных не подготовлена. " + ". ".join(problems) + ". "
                    "Из каталога проекта выполните: python -m alembic upgrade head "
                    "(в том же окружении и с тем же DATABASE_URL), затем повторите запуск."
                )

        async with self.engine.connect() as connection:
            await connection.run_sync(check)

    async def close(self):
        await self.engine.dispose()
