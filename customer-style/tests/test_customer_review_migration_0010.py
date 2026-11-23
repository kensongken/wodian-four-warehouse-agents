"""Published-0009 compatibility and 0010 recovery migration contracts."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from alembic import command
from opc_style.db import create_database_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_ID = "11111111-1111-1111-1111-111111111111"
JOB_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CANDIDATE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC).isoformat()
PUBLISHED_0009_ASSOCIATION_COLUMNS = {
    "id",
    "customer_id",
    "image_review_id",
    "image_candidate_id",
    "source_asset_id",
    "product_id",
    "image_kind",
    "created_at",
}
HARDENED_0010_ASSOCIATION_COLUMNS = PUBLISHED_0009_ASSOCIATION_COLUMNS | {
    "output_job_id",
    "product_asset_link_id",
}


def _config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _head_revision(config: Config) -> str:
    return ScriptDirectory.from_config(config).get_current_head()


def _schema_snapshot(connection: Connection) -> list[object]:
    return list(
        connection.execute(
            text(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ).all()
    )


def _insert_legacy_review(database_url: str, *, with_association: bool) -> None:
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": CUSTOMER_ID, "now": NOW},
        )
        if with_association:
            connection.execute(
                text(
                    "INSERT INTO product_assets "
                    "(id, customer_id, product_id, photo_id, identity, confirmed, "
                    "source_type, quality_flags, occurred_at, content_sha256, "
                    "mime_type, width, height, storage_path, created_at) VALUES "
                    "('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', :customer_id, "
                    "'legacy-product', 'legacy-photo', 'original_real_photo', 1, "
                    "'synthetic_metadata', '[]', :now, :digest, 'image/jpeg', "
                    "10, 10, NULL, :now)"
                ),
                {"customer_id": CUSTOMER_ID, "now": NOW, "digest": "a" * 64},
            )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, failure_reason, request_metadata, idempotency_key, "
                "created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', 'customer_approved', NULL, "
                "'{}', 'published-0009-review', :now, :now)"
            ),
            {"id": JOB_ID, "customer_id": CUSTOMER_ID, "now": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO image_candidates "
                "(id, customer_id, output_job_id, sequence_number, identity, "
                "artifact_path, content_sha256, mime_type, width, height, "
                "adapter_version, metadata, created_at) VALUES "
                "(:id, :customer_id, :job_id, 1, 'synthetic_style_image_candidate', "
                "'opaque', :digest, 'image/jpeg', 10, 10, "
                "'deterministic-local-image-v1', '{}', :now)"
            ),
            {
                "id": CANDIDATE_ID,
                "customer_id": CUSTOMER_ID,
                "job_id": JOB_ID,
                "digest": "b" * 64,
                "now": NOW,
            },
        )
        connection.execute(
            text(
                "INSERT INTO image_reviews "
                "(id, customer_id, image_candidate_id, decision, reason, "
                "idempotency_key, created_at) VALUES "
                "('dddddddd-dddd-dddd-dddd-dddddddddddd', :customer_id, "
                ":candidate_id, 'approved', NULL, 'published-0009-review', :now)"
            ),
            {
                "customer_id": CUSTOMER_ID,
                "candidate_id": CANDIDATE_ID,
                "now": NOW,
            },
        )
        if with_association:
            connection.execute(
                text(
                    "INSERT INTO approved_image_associations "
                    "(id, customer_id, image_review_id, image_candidate_id, "
                    "source_asset_id, product_id, image_kind, created_at) VALUES "
                    "('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', :customer_id, "
                    "'dddddddd-dddd-dddd-dddd-dddddddddddd', :candidate_id, "
                    "'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'legacy-product', "
                    "'style_image', :now)"
                ),
                {
                    "customer_id": CUSTOMER_ID,
                    "candidate_id": CANDIDATE_ID,
                    "now": NOW,
                },
            )
    engine.dispose()


def test_published_0009_schema_is_not_rewritten(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'published-0009.db'}"
    config = _config(database_url)

    command.upgrade(config, "0009_customer_image_review")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "authenticity_completions" not in inspector.get_table_names()
    assert {
        str(column["name"])
        for column in inspector.get_columns("approved_image_associations")
    } == PUBLISHED_0009_ASSOCIATION_COLUMNS
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0009_customer_image_review"
        )
    engine.dispose()


def test_empty_published_0009_upgrades_and_round_trips_through_0010(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty-0009.db'}"
    config = _config(database_url)
    command.upgrade(config, "0009_customer_image_review")

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "authenticity_completions" in inspector.get_table_names()
    assert {
        str(column["name"])
        for column in inspector.get_columns("approved_image_associations")
    } == HARDENED_0010_ASSOCIATION_COLUMNS
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == _head_revision(config)
        )
    engine.dispose()

    command.downgrade(config, "0009_customer_image_review")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "authenticity_completions" not in inspector.get_table_names()
    assert {
        str(column["name"])
        for column in inspector.get_columns("approved_image_associations")
    } == PUBLISHED_0009_ASSOCIATION_COLUMNS
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == _head_revision(config)
        )
    engine.dispose()


@pytest.mark.parametrize(
    "with_association", [False, True], ids=["review", "approved-association"]
)
def test_0010_refuses_published_0009_history_before_any_ddl(
    tmp_path: Path, with_association: bool
) -> None:
    database_url = f"sqlite:///{tmp_path / 'reviewed-0009.db'}"
    config = _config(database_url)
    command.upgrade(config, "0009_customer_image_review")
    _insert_legacy_review(database_url, with_association=with_association)
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        before = _schema_snapshot(connection)

    with pytest.raises(RuntimeError, match="0010.*legacy image reviews"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        after = _schema_snapshot(connection)
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0009_customer_image_review"
        )
    assert after == before
    engine.dispose()


def test_0010_upgrade_retries_after_empty_partial_ddl(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'partial-0010.db'}"
    config = _config(database_url)
    command.upgrade(config, "0009_customer_image_review")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE approved_image_associations"))
        connection.execute(
            text(
                "CREATE TABLE authenticity_completions "
                "(id VARCHAR(36) NOT NULL PRIMARY KEY)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_image_candidates_customer_id_job "
                "ON image_candidates (customer_id, id, output_job_id)"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        str(column["name"])
        for column in inspector.get_columns("approved_image_associations")
    } == HARDENED_0010_ASSOCIATION_COLUMNS
    assert {
        "customer_id",
        "output_job_id",
        "candidate_batch_digest",
        "previous_status",
        "result_status",
    } <= {
        str(column["name"])
        for column in inspector.get_columns("authenticity_completions")
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == _head_revision(config)
        )
    engine.dispose()


def test_0010_downgrade_retries_after_empty_partial_ddl(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'partial-0010-downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE approved_image_associations"))
        connection.execute(text("DROP TABLE authenticity_completions"))
        connection.execute(
            text("DROP INDEX uq_product_asset_links_customer_link_asset_job")
        )
        connection.execute(text("DROP INDEX uq_image_candidates_customer_id_job"))
    engine.dispose()

    command.downgrade(config, "0009_customer_image_review")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "authenticity_completions" not in inspector.get_table_names()
    assert {
        str(column["name"])
        for column in inspector.get_columns("approved_image_associations")
    } == PUBLISHED_0009_ASSOCIATION_COLUMNS
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0009_customer_image_review"
        )
    engine.dispose()
