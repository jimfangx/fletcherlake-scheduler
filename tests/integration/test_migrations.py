from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fl_scheduler.db.models import Base
from sqlalchemy import create_engine, text


def test_frozen_migrations_match_orm_and_upgrade_from_empty(postgres_url, monkeypatch) -> None:
    schema = f"migration_{uuid4().hex}"
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    url = postgres_url + f"&options=-csearch_path%3D{schema}"
    monkeypatch.setenv("FL_DATABASE_URL", url)
    config = Config(str(Path("services/scheduler/alembic.ini").resolve()))
    command.upgrade(config, "head")
    migrated = create_engine(url)
    try:
        with migrated.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
    finally:
        migrated.dispose()
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()
