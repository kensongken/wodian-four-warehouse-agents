"""Separate, fresh PostgreSQL lineage; never stamp an existing SQLite database."""

import os

from alembic import context
from opc_style.db import create_database_engine

config = context.config
url = os.environ.get("DATABASE_MIGRATION_URL") or config.get_main_option(
    "sqlalchemy.url"
)
if not url.startswith(("postgresql://", "postgresql+psycopg://")):
    raise RuntimeError("production_postgresql_required")

if context.is_offline_mode():
    context.configure(
        url=url, literal_binds=True, version_table="style_pg_alembic_version"
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_database_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection, version_table="style_pg_alembic_version"
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
