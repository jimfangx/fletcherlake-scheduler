"""FL_DATABASE_URL is injected by the service environment, never checked into configuration."""

import os

from alembic import context
from fl_scheduler.db.metadata import Base
from sqlalchemy import create_engine

url = os.environ["FL_DATABASE_URL"]

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
