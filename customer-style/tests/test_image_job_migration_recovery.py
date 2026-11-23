"""Interrupted SQLite image-job migrations remain safely retryable."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.mark.parametrize("status", ["queued", "generating", "cleanup_pending"])
def test_0008_upgrade_refuses_jobs_owned_by_an_older_local_executor(
    tmp_path: Path, status: str
) -> None:
    database_url = f"sqlite:///{tmp_path / f'active-{status}.db'}"
    config = _config(database_url)
    command.upgrade(config, "0007_image_job_recovery_scope")
    engine = create_engine(database_url)
    customer_id = "11111111-1111-1111-1111-111111111111"
    job_id = "33333333-3333-3333-3333-333333333333"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": "2026-08-31T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, failure_reason, request_metadata, idempotency_key, "
                "generation_claim_token, created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', :status, :reason, '{}', "
                "'active-upgrade-guard', 'old-executor', :now, :now)"
            ),
            {
                "id": job_id,
                "customer_id": customer_id,
                "status": status,
                "reason": (
                    "image_generation_failed;cleanup_pending:store_busy"
                    if status == "cleanup_pending"
                    else None
                ),
                "now": "2026-08-31T00:00:00+00:00",
            },
        )

    before_columns = {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    }
    with engine.connect() as connection:
        before_triggers = tuple(
            connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' ORDER BY name"
                )
            )
        )

    with pytest.raises(RuntimeError, match="queued, generating, or cleanup_pending"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0007_image_job_recovery_scope"
        )
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(image_output_jobs)"))
        }
        after_triggers = tuple(
            connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' ORDER BY name"
                )
            )
        )
    assert "artifact_store_capability" not in columns
    assert columns == before_columns
    assert after_triggers == before_triggers
    engine.dispose()


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "generating",
        "cleanup_pending",
        "cleanup_abandoned",
        "machine_check",
        "awaiting_customer_review",
    ],
)
def test_0008_downgrade_refuses_jobs_that_still_need_recovery_capability(
    tmp_path: Path, status: str
) -> None:
    database_url = f"sqlite:///{tmp_path / f'downgrade-guard-{status}.db'}"
    config = _config(database_url)
    command.upgrade(config, "0008_image_job_fencing_capability")
    engine = create_engine(database_url)
    customer_id = "11111111-1111-1111-1111-111111111111"
    job_id = "33333333-3333-3333-3333-333333333333"
    failure_reason = (
        f"image_generation_failed;{status}:store_busy"
        if status in {"cleanup_pending", "cleanup_abandoned"}
        else None
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": "2026-08-31T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, failure_reason, request_metadata, idempotency_key, "
                "artifact_store_capability, created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', :status, :reason, '{}', "
                "'downgrade-capability-guard', :capability, :now, :now)"
            ),
            {
                "id": job_id,
                "customer_id": customer_id,
                "status": status,
                "reason": failure_reason,
                "capability": "a" * 64,
                "now": "2026-08-31T00:00:00+00:00",
            },
        )
    before_columns = {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    }
    with engine.connect() as connection:
        before_triggers = tuple(
            connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' ORDER BY name"
                )
            )
        )

    with pytest.raises(RuntimeError, match="recovery capability"):
        command.downgrade(config, "0007_image_job_recovery_scope")

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0008_image_job_fencing_capability"
        )
        after_triggers = tuple(
            connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' ORDER BY name"
                )
            )
        )
    assert {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    } == before_columns
    assert after_triggers == before_triggers
    engine.dispose()


def test_image_job_migrations_round_trip_across_both_recovery_revisions(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'round-trip.db'}"
    config = _config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "0007_image_job_recovery_scope")
    command.upgrade(config, "head")
    command.downgrade(config, "0006_image_job_cleanup_pending")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    columns = {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    }
    assert {"generation_lease_expires_at", "artifact_store_capability"} <= columns
    engine.dispose()


@pytest.mark.parametrize("revision", ["0006", "0007", "0008"])
def test_upgrade_repairs_partially_applied_sqlite_migration(
    tmp_path: Path, revision: str
) -> None:
    database_url = f"sqlite:///{tmp_path / f'upgrade-{revision}.db'}"
    config = _config(database_url)
    engine = create_engine(database_url)
    predecessor = {"0006": "0005", "0007": "0006", "0008": "0007"}[revision]
    command.upgrade(config, predecessor)
    with engine.begin() as connection:
        connection.execute(
            text("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
        )
        connection.execute(
            text("DROP TRIGGER IF EXISTS trg_image_output_jobs_provenance_no_update")
        )
        if revision == "0007":
            connection.execute(
                text(
                    "ALTER TABLE image_output_jobs ADD COLUMN "
                    "generation_claim_token VARCHAR(36)"
                )
            )
        if revision == "0008":
            connection.execute(
                text(
                    "ALTER TABLE image_output_jobs ADD COLUMN "
                    "generation_lease_expires_at DATETIME"
                )
            )

    command.upgrade(config, revision)

    columns = {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    }
    if revision in {"0007", "0008"}:
        assert "generation_claim_token" in columns
    if revision == "0008":
        assert {"generation_lease_expires_at", "artifact_store_capability"} <= columns
    engine.dispose()


@pytest.mark.parametrize("revision", ["0007", "0008"])
def test_downgrade_repairs_partially_removed_sqlite_columns(
    tmp_path: Path, revision: str
) -> None:
    database_url = f"sqlite:///{tmp_path / f'downgrade-{revision}.db'}"
    config = _config(database_url)
    engine = create_engine(database_url)
    command.upgrade(config, revision)
    removed = (
        "artifact_store_capability"
        if revision == "0008"
        else "expected_artifact_sequences"
    )
    target = "0007" if revision == "0008" else "0006"
    with engine.begin() as connection:
        connection.execute(
            text("DROP TRIGGER IF EXISTS trg_image_output_jobs_provenance_no_update")
        )
        connection.execute(text(f"ALTER TABLE image_output_jobs DROP COLUMN {removed}"))

    command.downgrade(config, target)

    columns = {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    }
    assert removed not in columns
    engine.dispose()


@pytest.mark.parametrize(
    "removed_columns",
    [
        (),
        ("generation_lease_expires_at",),
        ("artifact_store_capability",),
        ("artifact_store_capability", "generation_lease_expires_at"),
    ],
)
def test_0008_downgrade_retry_preserves_legal_null_capability_machine_check(
    tmp_path: Path, removed_columns: tuple[str, ...]
) -> None:
    database_url = f"sqlite:///{tmp_path / ('null-cap-' + '-'.join(removed_columns) + '.db')}"
    config = _config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    customer_id = "11111111-1111-1111-1111-111111111111"
    job_id = "33333333-3333-3333-3333-333333333333"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": "2026-08-31T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, request_metadata, idempotency_key, "
                "artifact_store_capability, created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', 'machine_check', '{}', "
                "'null-capability-machine-check', NULL, :now, :now)"
            ),
            {
                "id": job_id,
                "customer_id": customer_id,
                "now": "2026-08-31T00:00:00+00:00",
            },
        )
        if removed_columns:
            connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_image_output_jobs_provenance_no_update"
                )
            )
        for column_name in removed_columns:
            connection.execute(
                text(f"ALTER TABLE image_output_jobs DROP COLUMN {column_name}")
            )

    command.downgrade(config, "0007_image_job_recovery_scope")

    columns = {
        item["name"] for item in inspect(engine).get_columns("image_output_jobs")
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0007_image_job_recovery_scope"
        )
        persisted = connection.execute(
            text("SELECT status FROM image_output_jobs WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
        provenance_trigger = connection.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_image_output_jobs_provenance_no_update'"
            )
        ).scalar_one()
    assert persisted == "machine_check"
    assert "artifact_store_capability" not in columns
    assert "generation_lease_expires_at" not in columns
    assert provenance_trigger == 1
    engine.dispose()
