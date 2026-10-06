"""Short-lived sessions and database-wide scheduling serialization."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .metadata import Base

PLACEMENT_LOCK = 0x464C5343


class Database:
    def __init__(self, url: str) -> None:
        if not url.startswith("postgresql"):
            raise ValueError("The scheduler requires PostgreSQL; agent persistence uses SQLite")
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self, placement: bool = False) -> Iterator[Session]:
        with self.sessions.begin() as session:
            if placement:
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PLACEMENT_LOCK})
            yield session

    def create_test_schema(self) -> None:
        """Tests use disposable PostgreSQL databases; production uses Alembic migrations."""
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()
