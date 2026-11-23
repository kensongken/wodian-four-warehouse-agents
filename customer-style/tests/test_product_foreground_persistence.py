"""Persistence proof for immutable pixel-preservation evidence."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from opc_style.models import (
    Base,
    PixelBackgroundRun,
    PixelCompositionReceipt,
    ProductForegroundMaskRevision,
    ProductForegroundVersion,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-27T00:00:00+00:00"


@pytest.fixture
def migrated_engine(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'foreground-evidence.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


def _seed_customer_asset_and_run(connection):
    customer_id, other_customer_id = str(uuid4()), str(uuid4())
    asset_id, run_id = str(uuid4()), str(uuid4())
    for identifier, label in (
        (customer_id, "Foreground customer"),
        (other_customer_id, "Other customer"),
    ):
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, :label, :now)"
            ),
            {"id": identifier, "label": label, "now": NOW},
        )
    connection.execute(
        text(
            "INSERT INTO product_assets "
            "(id, customer_id, product_id, photo_id, identity, confirmed, source_type, "
            "quality_flags, occurred_at, content_sha256, mime_type, width, height, created_at) "
            "VALUES (:id, :customer_id, 'bracelet-1', 'photo-1', "
            "'original_real_photo', 1, 'upload', '[]', :now, :digest, "
            "'image/png', 100, 100, :now)"
        ),
        {
            "id": asset_id,
            "customer_id": customer_id,
            "digest": "a" * 64,
            "now": NOW,
        },
    )
    connection.execute(
        text(
            "INSERT INTO style_suite_runs "
            "(id, customer_id, product_id, source_asset_id, source_content_sha256, "
            "style_count, slot_count, child_job_count, candidate_count, "
            "candidate_estimate_usd, maximum_estimated_cost_usd, cost_basis, currency, "
            "real_generation_enabled, candidate_count_per_item, idempotency_key, state, "
            "created_at) VALUES (:id, :customer_id, 'bracelet-1', :asset_id, :digest, "
            "1, 1, 1, 1, 0.12, 0.12, 'local_planning_estimate', 'USD', 1, 1, "
            "'pixel-run-1', 'planned', :now)"
        ),
        {
            "id": run_id,
            "customer_id": customer_id,
            "asset_id": asset_id,
            "digest": "a" * 64,
            "now": NOW,
        },
    )
    return customer_id, other_customer_id, asset_id, run_id


def _insert_foreground(
    connection,
    *,
    foreground_id,
    customer_id,
    asset_id,
    version=1,
    mask_digest="c" * 64,
):
    connection.execute(
        text(
            "INSERT INTO product_foreground_versions "
            "(id, customer_id, product_asset_id, product_id, version, "
            "source_content_sha256, foreground_sha256, mask_sha256, width, height, "
            "extractor_version, manifest_sha256, state, quality_flags, created_at, updated_at) "
            "VALUES (:id, :customer_id, :asset_id, 'bracelet-1', :version, :source, "
            ":foreground, :mask, 100, 100, 'fake-v1', :manifest, 'proposed', '[]', "
            ":now, :now)"
        ),
        {
            "id": foreground_id,
            "customer_id": customer_id,
            "asset_id": asset_id,
            "version": version,
            "source": "a" * 64,
            "foreground": "b" * 64,
            "mask": mask_digest,
            "manifest": "d" * 64,
            "now": NOW,
        },
    )


def test_pixel_preservation_models_are_registered() -> None:
    assert (
        ProductForegroundVersion.__table__
        is Base.metadata.tables["product_foreground_versions"]
    )
    assert PixelBackgroundRun.__table__ is Base.metadata.tables["pixel_background_runs"]
    assert (
        PixelCompositionReceipt.__table__
        is Base.metadata.tables["pixel_composition_receipts"]
    )
    assert (
        ProductForegroundMaskRevision.__table__
        is Base.metadata.tables["product_foreground_mask_revisions"]
    )


def _insert_mask_revision(
    connection,
    *,
    revision_id: str,
    customer_id: str,
    asset_id: str,
    base_id: str,
    result_id: str,
    idempotency_key: str = "mask-edit-1",
    editor_version: str = "human-mask-editor-v1",
    magic_wand_count: int = 1,
    edit_manifest_sha256: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO product_foreground_mask_revisions "
            "(id, customer_id, product_asset_id, base_foreground_version_id, "
            "result_foreground_version_id, source_content_sha256, base_mask_sha256, "
            "result_mask_sha256, edit_manifest_sha256, editor_version, "
            "magic_wand_count, erase_stroke_count, restore_stroke_count, "
            "added_pixel_count, deleted_pixel_count, result_pixel_count, "
            "component_count, idempotency_key, created_at) VALUES "
            "(:id, :customer_id, :asset_id, :base_id, :result_id, :source, :base_mask, "
            ":result_mask, :manifest, :editor, :magic, 0, 0, 0, 25, 75, 1, :key, :now)"
        ),
        {
            "id": revision_id,
            "customer_id": customer_id,
            "asset_id": asset_id,
            "base_id": base_id,
            "result_id": result_id,
            "source": "a" * 64,
            "base_mask": "c" * 64,
            "result_mask": "e" * 64,
            "manifest": edit_manifest_sha256 or "f" * 64,
            "editor": editor_version,
            "magic": magic_wand_count,
            "key": idempotency_key,
            "now": NOW,
        },
    )


def test_mask_revision_binds_same_customer_asset_base_and_result_versions(
    migrated_engine,
) -> None:
    with migrated_engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            customer_id, _, asset_id, _ = _seed_customer_asset_and_run(connection)
            base_id, result_id = str(uuid4()), str(uuid4())
            _insert_foreground(
                connection,
                foreground_id=base_id,
                customer_id=customer_id,
                asset_id=asset_id,
                version=1,
            )
            _insert_foreground(
                connection,
                foreground_id=result_id,
                customer_id=customer_id,
                asset_id=asset_id,
                version=2,
                mask_digest="e" * 64,
            )
            _insert_mask_revision(
                connection,
                revision_id=str(uuid4()),
                customer_id=customer_id,
                asset_id=asset_id,
                base_id=base_id,
                result_id=result_id,
            )
            with pytest.raises(IntegrityError, match="mask_revision_scope_mismatch"):
                _insert_mask_revision(
                    connection,
                    revision_id=str(uuid4()),
                    customer_id=customer_id,
                    asset_id=str(uuid4()),
                    base_id=base_id,
                    result_id=result_id,
                    idempotency_key="mask-edit-wrong-asset",
                )


def test_mask_revision_is_append_only_and_idempotency_scoped(migrated_engine) -> None:
    with migrated_engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            customer_id, _, asset_id, _ = _seed_customer_asset_and_run(connection)
            base_id, result_id, revision_id = str(uuid4()), str(uuid4()), str(uuid4())
            _insert_foreground(
                connection,
                foreground_id=base_id,
                customer_id=customer_id,
                asset_id=asset_id,
                version=1,
            )
            _insert_foreground(
                connection,
                foreground_id=result_id,
                customer_id=customer_id,
                asset_id=asset_id,
                version=2,
                mask_digest="e" * 64,
            )
            _insert_mask_revision(
                connection,
                revision_id=revision_id,
                customer_id=customer_id,
                asset_id=asset_id,
                base_id=base_id,
                result_id=result_id,
            )
            with pytest.raises(IntegrityError, match="mask_revision_immutable"):
                connection.execute(
                    text(
                        "UPDATE product_foreground_mask_revisions "
                        "SET component_count = 2 WHERE id = :id"
                    ),
                    {"id": revision_id},
                )
            with pytest.raises(IntegrityError):
                _insert_mask_revision(
                    connection,
                    revision_id=str(uuid4()),
                    customer_id=customer_id,
                    asset_id=asset_id,
                    base_id=base_id,
                    result_id=result_id,
                )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"same_result": True}, "ck_mask_revisions_distinct_versions"),
        ({"magic_wand_count": -1}, "ck_mask_revisions_operation_counts"),
        ({"editor_version": "human-mask-editor-v2"}, "ck_mask_revisions_editor"),
        ({"edit_manifest_sha256": "short"}, "ck_mask_revisions_digests"),
    ],
)
def test_mask_revision_rejects_invalid_digests_counts_and_editor_version(
    migrated_engine,
    overrides: dict[str, object],
    match: str,
) -> None:
    with migrated_engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            customer_id, _, asset_id, _ = _seed_customer_asset_and_run(connection)
            base_id, result_id = str(uuid4()), str(uuid4())
            _insert_foreground(
                connection,
                foreground_id=base_id,
                customer_id=customer_id,
                asset_id=asset_id,
                version=1,
            )
            _insert_foreground(
                connection,
                foreground_id=result_id,
                customer_id=customer_id,
                asset_id=asset_id,
                version=2,
                mask_digest="e" * 64,
            )
            if overrides.get("same_result"):
                result_id = base_id
            with pytest.raises(IntegrityError, match=match):
                _insert_mask_revision(
                    connection,
                    revision_id=str(uuid4()),
                    customer_id=customer_id,
                    asset_id=asset_id,
                    base_id=base_id,
                    result_id=result_id,
                    magic_wand_count=int(overrides.get("magic_wand_count", 1)),
                    editor_version=str(
                        overrides.get("editor_version", "human-mask-editor-v1")
                    ),
                    edit_manifest_sha256=str(
                        overrides.get("edit_manifest_sha256", "f" * 64)
                    ),
                )


def test_foreground_versions_are_scoped_append_only_and_decided_once(
    migrated_engine,
) -> None:
    with migrated_engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            customer_id, other_customer_id, asset_id, _ = _seed_customer_asset_and_run(
                connection
            )
            foreground_id = str(uuid4())
            _insert_foreground(
                connection,
                foreground_id=foreground_id,
                customer_id=customer_id,
                asset_id=asset_id,
            )
            connection.execute(
                text(
                    "UPDATE product_foreground_versions SET state = 'confirmed', "
                    "confirmation_key = 'confirm-1', decided_at = :now, updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": foreground_id, "now": NOW},
            )

            with pytest.raises(IntegrityError, match="product_foreground_immutable"):
                connection.execute(
                    text(
                        "UPDATE product_foreground_versions SET state = 'rejected' "
                        "WHERE id = :id"
                    ),
                    {"id": foreground_id},
                )
            with pytest.raises(IntegrityError, match="product_foreground_immutable"):
                connection.execute(
                    text("DELETE FROM product_foreground_versions WHERE id = :id"),
                    {"id": foreground_id},
                )
            with pytest.raises(IntegrityError):
                _insert_foreground(
                    connection,
                    foreground_id=str(uuid4()),
                    customer_id=customer_id,
                    asset_id=asset_id,
                )
            with pytest.raises(IntegrityError):
                _insert_foreground(
                    connection,
                    foreground_id=str(uuid4()),
                    customer_id=other_customer_id,
                    asset_id=asset_id,
                    version=2,
                )


def test_pipeline_evidence_downgrade_restores_foreground_update_guard(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'foreground-downgrade.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0041_observation_json_nulls")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            trigger = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND name = 'trg_product_foreground_update'"
                )
            ).scalar_one_or_none()
    finally:
        engine.dispose()

    assert trigger is not None
    assert "pipeline_manifest_sha256" not in trigger


def test_background_review_is_single_transition_and_attempts_are_bounded(
    migrated_engine,
) -> None:
    with migrated_engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            customer_id, _, asset_id, run_id = _seed_customer_asset_and_run(connection)
            foreground_id, background_id = str(uuid4()), str(uuid4())
            _insert_foreground(
                connection,
                foreground_id=foreground_id,
                customer_id=customer_id,
                asset_id=asset_id,
            )
            connection.execute(
                text(
                    "UPDATE product_foreground_versions SET state = 'confirmed', "
                    "confirmation_key = 'confirm-1', decided_at = :now, updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": foreground_id, "now": NOW},
            )
            connection.execute(
                text(
                    "INSERT INTO pixel_background_runs "
                    "(id, customer_id, style_suite_run_id, foreground_version_id, "
                    "authorized_manifest_sha256, authorized_manifest, idempotency_key, model, "
                    "maximum_usd, dispatch_state, outbound_attempts, result_sha256, "
                    "review_state, created_at, updated_at) VALUES "
                    "(:id, :customer_id, :run_id, :foreground_id, :manifest, :payload, "
                    "'background-1', 'fake-background-v1', 0.12, 'provider_responded', 1, "
                    ":result, 'awaiting_review', :now, :now)"
                ),
                {
                    "id": background_id,
                    "customer_id": customer_id,
                    "run_id": run_id,
                    "foreground_id": foreground_id,
                    "manifest": "e" * 64,
                    "payload": json.dumps({"style_refs": ["one", "two"]}),
                    "result": "f" * 64,
                    "now": NOW,
                },
            )
            connection.execute(
                text(
                    "UPDATE pixel_background_runs SET review_state = 'confirmed', "
                    "reviewed_at = :now, updated_at = :now WHERE id = :id"
                ),
                {"id": background_id, "now": NOW},
            )
            with pytest.raises(IntegrityError, match="pixel_background_run_immutable"):
                connection.execute(
                    text(
                        "UPDATE pixel_background_runs SET review_state = 'rejected' "
                        "WHERE id = :id"
                    ),
                    {"id": background_id},
                )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO pixel_background_runs "
                        "(id, customer_id, style_suite_run_id, foreground_version_id, "
                        "authorized_manifest_sha256, authorized_manifest, idempotency_key, model, "
                        "maximum_usd, dispatch_state, outbound_attempts, review_state, "
                        "created_at, updated_at) VALUES "
                        "(:id, :customer_id, :run_id, :foreground_id, :manifest, '{}', "
                        "'background-2', 'fake-background-v1', 0.12, 'reserved', 2, "
                        "'awaiting_review', :now, :now)"
                    ),
                    {
                        "id": str(uuid4()),
                        "customer_id": customer_id,
                        "run_id": run_id,
                        "foreground_id": foreground_id,
                        "manifest": "1" * 64,
                        "now": NOW,
                    },
                )


def test_composition_receipt_rejects_forged_artifact_digest(migrated_engine) -> None:
    with migrated_engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            customer_id, _, asset_id, run_id = _seed_customer_asset_and_run(connection)
            foreground_id, background_id = str(uuid4()), str(uuid4())
            job_id, candidate_id = str(uuid4()), str(uuid4())
            _insert_foreground(
                connection,
                foreground_id=foreground_id,
                customer_id=customer_id,
                asset_id=asset_id,
            )
            connection.execute(
                text(
                    "UPDATE product_foreground_versions SET state = 'confirmed', "
                    "confirmation_key = 'confirm-1', decided_at = :now, updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": foreground_id, "now": NOW},
            )
            connection.execute(
                text(
                    "INSERT INTO pixel_background_runs "
                    "(id, customer_id, style_suite_run_id, foreground_version_id, "
                    "authorized_manifest_sha256, authorized_manifest, idempotency_key, model, "
                    "maximum_usd, dispatch_state, outbound_attempts, result_sha256, "
                    "review_state, reviewed_at, created_at, updated_at) VALUES "
                    "(:id, :customer_id, :run_id, :foreground_id, :manifest, '{}', "
                    "'background-1', 'fake-background-v1', 0.12, 'provider_responded', 1, "
                    ":result, 'confirmed', :now, :now, :now)"
                ),
                {
                    "id": background_id,
                    "customer_id": customer_id,
                    "run_id": run_id,
                    "foreground_id": foreground_id,
                    "manifest": "e" * 64,
                    "result": "f" * 64,
                    "now": NOW,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO image_output_jobs "
                    "(id, customer_id, output_type, output_ratio, adapter_version, status, "
                    "request_metadata, idempotency_key, created_at, updated_at) VALUES "
                    "(:id, :customer_id, 'product_style', '1:1', 'pixel-compositor-v1', "
                    "'awaiting_review', '{}', 'pixel-output-1', :now, :now)"
                ),
                {"id": job_id, "customer_id": customer_id, "now": NOW},
            )
            connection.execute(
                text(
                    "INSERT INTO image_candidates "
                    "(id, customer_id, output_job_id, sequence_number, identity, artifact_path, "
                    "content_sha256, mime_type, width, height, adapter_version, metadata, created_at) "
                    "VALUES (:id, :customer_id, :job_id, 1, 'derived', 'private/result.png', "
                    ":digest, 'image/png', 100, 100, 'pixel-compositor-v1', '{}', :now)"
                ),
                {
                    "id": candidate_id,
                    "customer_id": customer_id,
                    "job_id": job_id,
                    "digest": "9" * 64,
                    "now": NOW,
                },
            )
            with pytest.raises(
                IntegrityError, match="pixel_composition_receipt_mismatch"
            ):
                connection.execute(
                    text(
                        "INSERT INTO pixel_composition_receipts "
                        "(id, customer_id, foreground_version_id, background_run_id, "
                        "image_candidate_id, transform_payload, compositor_version, "
                        "verification_state, foreground_sha256, background_sha256, "
                        "result_sha256, transformed_alpha_sha256, foreground_rgb_sha256, "
                        "publishable, created_at) VALUES "
                        "(:id, :customer_id, :foreground_id, :background_id, :candidate_id, "
                        "'{}', 'pixel-compositor-v1', 'verified', :foreground, :background, "
                        ":forged_result, :alpha, :rgb, 0, :now)"
                    ),
                    {
                        "id": str(uuid4()),
                        "customer_id": customer_id,
                        "foreground_id": foreground_id,
                        "background_id": background_id,
                        "candidate_id": candidate_id,
                        "foreground": "b" * 64,
                        "background": "f" * 64,
                        "forged_result": "8" * 64,
                        "alpha": "7" * 64,
                        "rgb": "6" * 64,
                        "now": NOW,
                    },
                )


def test_migration_exposes_expected_constraints_and_triggers(migrated_engine) -> None:
    inspector = inspect(migrated_engine)
    assert {
        "product_foreground_versions",
        "pixel_background_runs",
        "pixel_composition_receipts",
    } <= set(inspector.get_table_names())
    foreground_checks = " ".join(
        constraint["sqltext"] or ""
        for constraint in inspector.get_check_constraints("product_foreground_versions")
    )
    background_checks = " ".join(
        constraint["sqltext"] or ""
        for constraint in inspector.get_check_constraints("pixel_background_runs")
    )
    background_foreign_keys = inspector.get_foreign_keys("pixel_background_runs")
    with migrated_engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
    assert {
        "trg_product_foreground_update",
        "trg_product_foreground_no_delete",
        "trg_pixel_background_update",
        "trg_pixel_background_no_delete",
        "trg_pixel_composition_receipt_facts",
        "trg_pixel_composition_receipt_immutable",
        "trg_pixel_composition_receipt_no_delete",
    } <= triggers
    assert "length(source_content_sha256) = 64" in foreground_checks
    assert "state IN ('proposed', 'confirmed', 'rejected')" in foreground_checks
    assert (
        "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')"
        in background_checks
    )
    assert "review_state IN ('awaiting_review', 'confirmed', 'rejected')" in (
        background_checks
    )
    assert "outbound_attempts IN (0, 1)" in background_checks
    assert any(
        foreign_key["constrained_columns"] == ["customer_id", "foreground_version_id"]
        and foreign_key["referred_table"] == "product_foreground_versions"
        for foreign_key in background_foreign_keys
    )
