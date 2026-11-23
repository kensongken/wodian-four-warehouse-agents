"""Both legacy histories must reach one schema without losing owner records."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import insert, inspect, select, text

from alembic import command
from opc_style.db import create_database_engine
from opc_style.models import Base, Customer


@pytest.mark.parametrize(
    "source_revision",
    [
        "0024_scene_style_origin_scope",
        "0042_foreground_pipeline_evidence",
        "0030_mall_style_executor",
    ],
)
def test_unified_upgrade_preserves_both_histories_and_customer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_revision: str
) -> None:
    url = f"sqlite:///{tmp_path / 'unified.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, source_revision)
    engine = create_database_engine(url)
    customer_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(Customer).values(
                    id=customer_id,
                    display_name="existing-owner",
                    created_at=datetime.now(UTC),
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(
                select(Customer.display_name).where(Customer.id == customer_id)
            ) == "existing-owner"
            assert connection.scalars(
                text("SELECT version_num FROM alembic_version")
            ).all() == ["0046_foreground_disagreement_evidence"]
        schema = inspect(engine)
        assert set(Base.metadata.tables) <= set(schema.get_table_names())
        for table in Base.metadata.sorted_tables:
            actual = {column["name"] for column in schema.get_columns(table.name)}
            assert set(table.columns.keys()) <= actual, table.name
    finally:
        engine.dispose()
