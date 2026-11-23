"""Regression coverage for the isolated clean-database migration verifier."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from opc_style.models import Base

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_recovery_and_style_evidence_schema_is_registered() -> None:
    assert "asset_store_recovery_bindings" in Base.metadata.tables
    assert "style_field_evidence" in Base.metadata.tables
    assert "product_foreground_versions" in Base.metadata.tables
    assert "pixel_background_runs" in Base.metadata.tables
    assert "pixel_composition_receipts" in Base.metadata.tables
    assert "product_foreground_mask_revisions" in Base.metadata.tables
    assert "foreground_benchmark_runs" in Base.metadata.tables
    assert "foreground_benchmark_sample_results" in Base.metadata.tables
    assert "foreground_shadow_decisions" in Base.metadata.tables


def test_recovery_and_style_evidence_migration_is_append_only(tmp_path: Path) -> None:
    database_path = tmp_path / "recovery-evidence.db"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    customer_id = str(uuid4())
    binding_id = str(uuid4())
    now = "2026-09-12T00:00:00+00:00"
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        assert {"asset_store_recovery_bindings", "style_field_evidence"} <= tables
        assert {
            "trg_asset_store_recovery_binding_immutable",
            "trg_asset_store_recovery_binding_no_delete",
            "trg_style_field_evidence_immutable",
            "trg_style_field_evidence_no_delete",
        } <= triggers
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Recovery customer', :now)"
            ),
            {"id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO asset_store_recovery_bindings "
                "(id, customer_id, source_namespace, target_namespace, sample_count, "
                "manifest_sha256, source_label, status, created_at) VALUES "
                "(:id, :customer_id, :source_namespace, :target_namespace, 10, "
                ":manifest_sha256, 'backup-2026-09-12', 'active', :now)"
            ),
            {
                "id": binding_id,
                "customer_id": customer_id,
                "source_namespace": "a" * 64,
                "target_namespace": "b" * 64,
                "manifest_sha256": "c" * 64,
                "now": now,
            },
        )
        with pytest.raises(Exception, match="asset_store_recovery_binding_immutable"):
            connection.execute(
                text(
                    "UPDATE asset_store_recovery_bindings SET sample_count = 9 "
                    "WHERE id = :id"
                ),
                {"id": binding_id},
            )
    engine.dispose()


def test_clean_migration_verifier_uses_an_isolated_temporary_database() -> None:
    """The verifier reaches Alembic head without changing the normal local database."""
    normal_database = REPOSITORY_ROOT / "opc_style.db"
    normal_database_state = (
        normal_database.stat().st_size if normal_database.exists() else None
    )

    result = subprocess.run(
        [sys.executable, "scripts/verify_clean_migration.py"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "clean_migration_verifier=PASS" in result.stdout
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    assert f"verified_revision={expected_head}" in result.stdout
    assert (
        normal_database.stat().st_size if normal_database.exists() else None
    ) == normal_database_state


def test_style_sample_content_migration_seals_media_facts(tmp_path: Path) -> None:
    """A missing seal column or mutable media identity must fail this migration test."""
    database_path = tmp_path / "style-sample-content.db"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                text("PRAGMA table_info(style_samples)")
            ).mappings()
        }
        triggers = {
            row["name"]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).mappings()
        }
    engine.dispose()

    assert {
        "content_sha256",
        "mime_type",
        "width",
        "height",
        "asset_store_namespace",
        "consent_text_version",
    } <= columns
    assert "trg_style_samples_content_immutable" in triggers


def test_scene_style_origin_migration_backfills_and_scopes_active_names(
    tmp_path: Path,
) -> None:
    """Old rows retain their inferred origin and only collide within that origin."""
    database_path = tmp_path / "scene-style-origin.db"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "0023_real_pilot_authorization_ledger")

    customer_id = str(uuid4())
    demo_card_id, customer_card_id = str(uuid4()), str(uuid4())
    demo_scene_id, customer_scene_id = str(uuid4()), str(uuid4())
    now = "2026-09-10T00:00:00+00:00"
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Migration customer', :now)"
            ),
            {"id": customer_id, "now": now},
        )
        for card_id, scene_id, name, content in (
            (
                demo_card_id,
                demo_scene_id,
                "同名暖光",
                {"historical_style": True, "style_origin": "local_demo"},
            ),
            (
                customer_card_id,
                customer_scene_id,
                "旧真实风格",
                {"historical_style": True},
            ),
        ):
            connection.execute(
                text(
                    "INSERT INTO style_cards "
                    "(id, customer_id, kind, active, created_at) "
                    "VALUES (:id, :customer_id, 'scene', 1, :now)"
                ),
                {"id": card_id, "customer_id": customer_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO scene_styles "
                    "(id, customer_id, style_card_id, name, active, rules, created_at) "
                    "VALUES (:id, :customer_id, :card_id, :name, 1, '{}', :now)"
                ),
                {
                    "id": scene_id,
                    "customer_id": customer_id,
                    "card_id": card_id,
                    "name": name,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO style_card_versions "
                    "(id, customer_id, style_card_id, version, content, created_at) "
                    "VALUES (:id, :customer_id, :card_id, 1, :content, :now)"
                ),
                {
                    "id": str(uuid4()),
                    "customer_id": customer_id,
                    "card_id": card_id,
                    "content": json.dumps(content),
                    "now": now,
                },
            )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        origins = connection.execute(
            text(
                "SELECT name, style_origin FROM scene_styles "
                "ORDER BY style_origin, name"
            )
        ).all()
        indexes = {
            row[1]
            for row in connection.execute(text("PRAGMA index_list(scene_styles)"))
        }
        assert origins == [
            ("旧真实风格", "customer_upload"),
            ("同名暖光", "local_demo"),
        ]
        assert "uq_scene_styles_active_customer_origin_name" in indexes
        assert "uq_scene_styles_active_customer_name" not in indexes

        sibling_card_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO style_cards "
                "(id, customer_id, kind, active, created_at) "
                "VALUES (:id, :customer_id, 'scene', 1, :now)"
            ),
            {"id": sibling_card_id, "customer_id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO scene_styles "
                "(id, customer_id, style_card_id, name, style_origin, active, rules, created_at) "
                "VALUES (:id, :customer_id, :card_id, '同名暖光', "
                "'customer_upload', 1, '{}', :now)"
            ),
            {
                "id": str(uuid4()),
                "customer_id": customer_id,
                "card_id": sibling_card_id,
                "now": now,
            },
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO scene_styles "
                    "(id, customer_id, style_card_id, name, style_origin, active, rules, created_at) "
                    "VALUES (:id, :customer_id, :card_id, '同名暖光', "
                    "'local_demo', 1, '{}', :now)"
                ),
                {
                    "id": str(uuid4()),
                    "customer_id": customer_id,
                    "card_id": sibling_card_id,
                    "now": now,
                },
            )
    engine.dispose()


def test_style_sample_content_guard_rejects_direct_consent_or_media_mutation(
    tmp_path: Path,
) -> None:
    """A direct SQL rewrite of legal or media evidence must be rejected by SQLite."""
    database_path = tmp_path / "style-sample-content-guard.db"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    customer_id = str(uuid4())
    sample_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": "2026-09-08T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO style_samples "
                "(id, customer_id, photo_id, authorized_at, consent_provenance, "
                "consent_text_version, content_sha256, mime_type, width, height, "
                "asset_store_namespace, created_at) VALUES "
                "(:id, :customer_id, 'history-1', :now, 'customer_opt_in', "
                "'style-consent-v1', :hash, 'image/jpeg', 10, 10, 'private-store', :now)"
            ),
            {
                "id": sample_id,
                "customer_id": customer_id,
                "now": "2026-09-08T00:00:00+00:00",
                "hash": "a" * 64,
            },
        )
        with pytest.raises(Exception, match="style_sample_content_immutable"):
            connection.execute(
                text(
                    "UPDATE style_samples SET consent_text_version = 'style-consent-v2' "
                    "WHERE id = :id"
                ),
                {"id": sample_id},
            )
    engine.dispose()


def test_failed_0017_reconciliation_keeps_0016_retryable_and_guarded(
    tmp_path: Path,
) -> None:
    """A missing legal audit must not leave SQLite halfway through migration 0017."""
    database_path = tmp_path / "style-sample-consent-retry.db"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "0016_style_sample_content")

    customer_id = str(uuid4())
    sample_id = str(uuid4())
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": "2026-09-08T00:00:00+00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO style_samples "
                "(id, customer_id, photo_id, authorized_at, consent_provenance, "
                "content_sha256, mime_type, width, height, asset_store_namespace, "
                "created_at) VALUES "
                "(:id, :customer_id, 'history-1', :now, 'customer_opt_in', "
                ":hash, 'image/jpeg', 10, 10, 'private-store', :now)"
            ),
            {
                "id": sample_id,
                "customer_id": customer_id,
                "now": "2026-09-08T00:00:00+00:00",
                "hash": "a" * 64,
            },
        )

    with pytest.raises(
        RuntimeError,
        match="sealed style sample consent text version cannot be recovered",
    ):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        columns = {
            row["name"]
            for row in connection.execute(
                text("PRAGMA table_info(style_samples)")
            ).mappings()
        }
        triggers = {
            row["name"]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).mappings()
        }
    assert version == "0016_style_sample_content"
    assert "consent_text_version" not in columns
    assert "trg_style_samples_content_immutable" in triggers

    with engine.begin() as connection:
        with pytest.raises(Exception, match="style_sample_content_immutable"):
            connection.execute(
                text("UPDATE style_samples SET width = 11 WHERE id = :id"),
                {"id": sample_id},
            )
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(id, customer_id, event_type, payload, occurred_at) VALUES "
                "(:id, :customer_id, 'style_sample.content_authorized', :payload, :now)"
            ),
            {
                "id": str(uuid4()),
                "customer_id": customer_id,
                "payload": json.dumps(
                    {
                        "sample_id": sample_id,
                        "consent_text_version": "style-consent-v1",
                    }
                ),
                "now": "2026-09-08T00:00:00+00:00",
            },
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        version_value = connection.execute(
            text("SELECT consent_text_version FROM style_samples WHERE id = :id"),
            {"id": sample_id},
        ).scalar_one()
    engine.dispose()

    assert version == ScriptDirectory.from_config(config).get_current_head()
    assert version_value == "style-consent-v1"


def test_0006_downgrade_refuses_to_orphan_cleanup_pending_jobs(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "downgrade-guard.db"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "0006_image_job_cleanup_pending")
    customer_id = str(uuid4())
    job_id = str(uuid4())
    engine = create_engine(f"sqlite:///{database_path}")
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
                "created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', 'cleanup_pending', "
                "'image_generation_failed;cleanup_pending:store_busy', '{}', "
                "'downgrade-guard', :now, :now)"
            ),
            {
                "id": job_id,
                "customer_id": customer_id,
                "now": "2026-08-31T00:00:00+00:00",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="cleanup_pending"):
        command.downgrade(config, "0005_product_asset_intake_identity")

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0006_image_job_cleanup_pending"
        )
        assert (
            connection.execute(
                text("SELECT status FROM image_output_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one()
            == "cleanup_pending"
        )
    engine.dispose()
