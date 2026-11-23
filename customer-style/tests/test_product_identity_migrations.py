"""Migration coverage for durable product identity analysis and card versions."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'identity.db'}")
    return config


def _table_names(config: Config) -> set[str]:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_manual_identity_migration_allows_zero_cost_runs(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        constraints = inspect(engine).get_check_constraints(
            "product_identity_analysis_runs"
        )
    finally:
        engine.dispose()

    checks = {
        item["name"]: item["sqltext"].replace(" ", "") for item in constraints
    }
    assert checks["ck_product_identity_analysis_usd_nonnegative"] == (
        "authorized_usd>=0"
    )


def test_identity_observation_tables_upgrade_and_round_trip(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0037_foreground_benchmark_shadow_gate")
    command.upgrade(alembic_config, "0038_product_identity_observations")


def test_identity_observation_lifecycle_trigger_allows_sealing_then_freezes(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0039_product_identity_observation_lifecycle")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))

    @event.listens_for(engine, "connect")
    def _disable_foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    run_id = str(uuid4())
    now = "2026-09-28T00:00:00+00:00"
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER trg_product_identity_observation_scope")
            )
            connection.execute(
                text(
                    "INSERT INTO product_identity_observation_runs "
                    "(id, customer_id, product_asset_id, product_id, "
                    "source_content_sha256, product_identity_card_version_id, "
                    "product_identity_card_sha256, idempotency_key, observer, "
                    "observer_version, observation_mode, policy_version, "
                    "authorized_usd, input_manifest_sha256, input_manifest, "
                    "dispatch_state, outbound_attempts, created_at, updated_at) "
                    "VALUES (:id, :customer, :asset, 'product-a', :source_sha, "
                    ":card, :card_sha, 'key-a', 'observer-a', 'v1', 'mode-a', "
                    "'policy-a', 0.10, :manifest_sha, '{}', 'reserved', 0, :now, :now)"
                ),
                {
                    "id": run_id,
                    "customer": str(uuid4()),
                    "asset": str(uuid4()),
                    "card": str(uuid4()),
                    "source_sha": "a" * 64,
                    "card_sha": "b" * 64,
                    "manifest_sha": "c" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "CREATE TRIGGER trg_product_identity_observation_scope BEFORE INSERT ON "
                    "product_identity_observation_runs WHEN NOT EXISTS (SELECT 1 FROM "
                    "product_assets a WHERE a.customer_id = NEW.customer_id AND "
                    "a.id = NEW.product_asset_id AND a.product_id = NEW.product_id AND "
                    "a.content_sha256 = NEW.source_content_sha256 AND a.confirmed = 1 AND "
                    "a.identity = 'original_real_photo') BEGIN SELECT "
                    "RAISE(ABORT, 'product_identity_observation_scope_mismatch'); END"
                )
            )
            connection.execute(
                text(
                    "UPDATE product_identity_observation_runs SET "
                    "dispatch_state='dispatch_uncertain', outbound_attempts=1, "
                    "updated_at=:now WHERE id=:id"
                ),
                {"id": run_id, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE product_identity_observation_runs SET "
                    "dispatch_state='provider_responded', observation_payload='{}', "
                    "decision='pass', reason_codes='[]', result_sha256=:result_sha, "
                    "updated_at=:now WHERE id=:id"
                ),
                {"id": run_id, "result_sha": "d" * 64, "now": now},
            )
            with pytest.raises(IntegrityError, match="product_identity_observation_immutable"):
                connection.execute(
                    text(
                        "UPDATE product_identity_observation_runs "
                        "SET decision='block' WHERE id=:id"
                    ),
                    {"id": run_id},
                )
    finally:
        engine.dispose()
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        inspector = inspect(engine)
        assert {
            "product_identity_observation_runs",
            "product_identity_observation_reviews",
        } <= set(inspector.get_table_names())
        assert {
            "product_identity_observation_run_id",
            "product_identity_observation_result_sha256",
            "product_identity_observation_review_id",
        } <= {column["name"] for column in inspector.get_columns("style_suite_runs")}
        with engine.connect() as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                )
            }
        assert {
            "trg_product_identity_observation_scope",
            "trg_product_identity_observation_immutable",
            "trg_product_identity_observation_review_immutable",
            "trg_style_suite_identity_observation_complete",
        } <= triggers
    finally:
        engine.dispose()

    command.downgrade(alembic_config, "0038_product_identity_observations")
    assert "product_identity_observation_runs" in _table_names(alembic_config)


def test_local_identity_verification_constraints_allow_zero_cost_without_dispatch(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0040_local_identity_verification")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    checks = {
        item["name"]: item["sqltext"]
        for item in inspect(engine).get_check_constraints(
            "product_identity_observation_runs"
        )
    }
    try:
        assert ">= 0" in checks["ck_product_identity_observation_usd_nonnegative"]
        assert "locally_verified" in checks[
            "ck_product_identity_observation_dispatch_state"
        ]
    finally:
        engine.dispose()


def test_head_normalizes_json_null_before_observation_dispatch(
    alembic_config: Config,
) -> None:
    """ORM JSON None must not block the reserved -> dispatch transition."""
    command.upgrade(alembic_config, "0040_local_identity_verification")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))

    @event.listens_for(engine, "connect")
    def _disable_foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    run_id = str(uuid4())
    now = "2026-09-28T00:00:00+00:00"
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER trg_product_identity_observation_scope")
            )
            connection.execute(
                text(
                    "INSERT INTO product_identity_observation_runs "
                    "(id, customer_id, product_asset_id, product_id, "
                    "source_content_sha256, product_identity_card_version_id, "
                    "product_identity_card_sha256, idempotency_key, observer, "
                    "observer_version, observation_mode, policy_version, "
                    "authorized_usd, input_manifest_sha256, input_manifest, "
                    "dispatch_state, outbound_attempts, observation_payload, "
                    "reason_codes, created_at, updated_at) "
                    "VALUES (:id, :customer, :asset, 'product-a', :source_sha, "
                    ":card, :card_sha, 'key-json-null', 'observer-a', 'v1', "
                    "'mode-a', 'policy-a', 0.10, :manifest_sha, '{}', "
                    "'reserved', 0, 'null', 'null', :now, :now)"
                ),
                {
                    "id": run_id,
                    "customer": str(uuid4()),
                    "asset": str(uuid4()),
                    "card": str(uuid4()),
                    "source_sha": "a" * 64,
                    "card_sha": "b" * 64,
                    "manifest_sha": "c" * 64,
                    "now": now,
                },
            )
        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE product_identity_observation_runs SET "
                    "dispatch_state='dispatch_uncertain', outbound_attempts=1, "
                    "updated_at=:now WHERE id=:id"
                ),
                {"id": run_id, "now": now},
            )
            row = connection.execute(
                text(
                    "SELECT observation_payload, reason_codes, dispatch_state, "
                    "outbound_attempts FROM product_identity_observation_runs WHERE id=:id"
                ),
                {"id": run_id},
            ).one()
        assert row == (None, None, "dispatch_uncertain", 1)
    finally:
        engine.dispose()


def test_identity_card_tables_upgrade_from_0029_and_round_trip(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0029_fidelity_warning_acknowledgement")
    command.upgrade(alembic_config, "0030_product_identity_cards")
    assert {
        "product_identity_analysis_runs",
        "product_identity_card_versions",
    } <= _table_names(alembic_config)

    command.downgrade(alembic_config, "0029_fidelity_warning_acknowledgement")
    assert "product_identity_analysis_runs" not in _table_names(alembic_config)
    assert "product_identity_card_versions" not in _table_names(alembic_config)
    command.upgrade(alembic_config, "0030_product_identity_cards")


def test_style_suite_identity_upgrade_preserves_a_populated_run_graph(
    alembic_config: Config,
) -> None:
    """SQLite must upgrade a parent run even when immutable child rows reference it."""
    command.upgrade(alembic_config, "0030_product_identity_cards")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    customer_id = str(uuid4())
    asset_id = str(uuid4())
    run_id = str(uuid4())
    now = "2026-09-14T00:00:00+00:00"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO customers (id, display_name, created_at) "
                    "VALUES (:id, 'Upgrade customer', :now)"
                ),
                {"id": customer_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO product_assets "
                    "(id, customer_id, product_id, photo_id, identity, confirmed, "
                    "source_type, quality_flags, occurred_at, content_sha256, mime_type, "
                    "width, height, storage_path, created_at) VALUES "
                    "(:id, :customer_id, 'product-a', 'photo-a', 'original_real_photo', "
                    "1, 'classification_handoff', '[]', :now, :sha256, 'image/png', "
                    "10, 10, 'asset.png', :now)"
                ),
                {
                    "id": asset_id,
                    "customer_id": customer_id,
                    "sha256": "a" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO style_suite_runs "
                    "(id, customer_id, product_id, source_asset_id, source_content_sha256, "
                    "style_count, slot_count, child_job_count, candidate_count, "
                    "candidate_estimate_usd, maximum_estimated_cost_usd, cost_basis, "
                    "currency, real_generation_enabled, state, created_at, "
                    "idempotency_key, pilot_scope) VALUES "
                    "(:id, :customer_id, 'product-a', :asset_id, :sha256, 1, 1, 1, 3, "
                    "0.1, 0.3, 'local_planning_estimate', 'USD', 1, 'planned', :now, "
                    "'upgrade-run', '{}')"
                ),
                {
                    "id": run_id,
                    "customer_id": customer_id,
                    "asset_id": asset_id,
                    "sha256": "a" * 64,
                    "now": now,
                },
            )
            # This fixture needs only a durable child foreign key. The snapshot
            # trigger is orthogonal to the parent-table rebuild under test.
            connection.execute(text("DROP TRIGGER trg_style_suite_completion_insert"))
            connection.execute(
                text(
                    "INSERT INTO style_suite_completions "
                    "(id, customer_id, run_id, version, snapshot, created_at) VALUES "
                    "(:id, :customer_id, :run_id, 1, '{}', :now)"
                ),
                {
                    "id": str(uuid4()),
                    "customer_id": customer_id,
                    "run_id": run_id,
                    "now": now,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(alembic_config, "0031_style_suite_identity_card")

    upgraded = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        with upgraded.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM style_suite_runs")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM style_suite_completions")
                ).scalar_one()
                == 1
            )
            assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    finally:
        upgraded.dispose()


def test_identity_database_guards_freeze_scope_and_confirmed_cards(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0031_style_suite_identity_card")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    customer_id = str(uuid4())
    asset_id = str(uuid4())
    other_asset_id = str(uuid4())
    run_id = str(uuid4())
    card_id = str(uuid4())
    suite_run_id = str(uuid4())
    now = "2026-09-12T00:00:00+00:00"
    try:
        with engine.begin() as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                )
            }
            assert {
                "trg_product_identity_analysis_scope_immutable",
                "trg_product_identity_analysis_no_delete",
                "trg_product_identity_card_immutable",
                "trg_product_identity_card_no_delete",
            } <= triggers
            connection.execute(
                text(
                    "INSERT INTO customers (id, display_name, created_at) "
                    "VALUES (:id, 'Identity customer', :now)"
                ),
                {"id": customer_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO product_assets "
                    "(id, customer_id, product_id, photo_id, identity, confirmed, "
                    "source_type, quality_flags, occurred_at, content_sha256, mime_type, "
                    "width, height, storage_path, created_at) VALUES "
                    "(:id, :customer_id, 'product-a', 'photo-a', "
                    "'original_real_photo', 1, 'classification_handoff', '[]', :now, "
                    ":sha256, 'image/png', 10, 10, 'asset.png', :now)"
                ),
                {
                    "id": asset_id,
                    "customer_id": customer_id,
                    "sha256": "a" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO product_assets "
                    "(id, customer_id, product_id, photo_id, identity, confirmed, "
                    "source_type, quality_flags, occurred_at, content_sha256, mime_type, "
                    "width, height, storage_path, created_at) VALUES "
                    "(:id, :customer_id, 'product-b', 'photo-b', "
                    "'original_real_photo', 1, 'classification_handoff', '[]', :now, "
                    ":sha256, 'image/png', 10, 10, 'asset-b.png', :now)"
                ),
                {
                    "id": other_asset_id,
                    "customer_id": customer_id,
                    "sha256": "e" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO product_identity_analysis_runs "
                    "(id, customer_id, product_asset_id, product_id, "
                    "source_content_sha256, idempotency_key, model, authorized_usd, "
                    "input_manifest_sha256, input_manifest, dispatch_state, "
                    "outbound_attempts, proposal, failure_code, created_at, updated_at) "
                    "VALUES (:id, :customer_id, :asset_id, 'product-a', :sha256, "
                    "'identity-1', 'controlled-model', 0.5, :manifest_sha256, '{}', "
                    "'reserved', 0, NULL, NULL, :now, :now)"
                ),
                {
                    "id": run_id,
                    "customer_id": customer_id,
                    "asset_id": asset_id,
                    "sha256": "a" * 64,
                    "manifest_sha256": "b" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE product_identity_analysis_runs SET dispatch_state = "
                    "'provider_responded', outbound_attempts = 1, proposal = '{}' "
                    "WHERE id = :id"
                ),
                {"id": run_id},
            )
            connection.execute(
                text(
                    "INSERT INTO product_identity_card_versions "
                    "(id, customer_id, product_asset_id, product_id, analysis_run_id, "
                    "version, source_content_sha256, card_payload, card_sha256, "
                    "confirmed_by, created_at) VALUES "
                    "(:id, :customer_id, :asset_id, 'product-a', :run_id, 1, "
                    ":source_sha256, '{}', :card_sha256, 'local_customer', :now)"
                ),
                {
                    "id": card_id,
                    "customer_id": customer_id,
                    "asset_id": asset_id,
                    "run_id": run_id,
                    "source_sha256": "a" * 64,
                    "card_sha256": "c" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO style_suite_runs "
                    "(id, customer_id, product_id, source_asset_id, "
                    "source_content_sha256, style_count, slot_count, "
                    "child_job_count, candidate_count, candidate_estimate_usd, "
                    "maximum_estimated_cost_usd, cost_basis, currency, "
                    "real_generation_enabled, state, created_at, idempotency_key, "
                    "pilot_scope, product_identity_card_version_id, "
                    "product_identity_card_sha256) VALUES "
                    "(:id, :customer_id, 'product-a', :asset_id, :source_sha256, "
                    "1, 1, 1, 3, 0.1, 0.3, 'local_planning_estimate', 'USD', "
                    "1, 'planned', :now, 'suite-identity-1', '{}', :card_id, "
                    ":card_sha256)"
                ),
                {
                    "id": suite_run_id,
                    "customer_id": customer_id,
                    "asset_id": asset_id,
                    "source_sha256": "a" * 64,
                    "now": now,
                    "card_id": card_id,
                    "card_sha256": "c" * 64,
                },
            )

        with (
            engine.begin() as connection,
            pytest.raises(IntegrityError),
        ):
            connection.execute(
                text(
                    "INSERT INTO style_suite_runs "
                    "(id, customer_id, product_id, source_asset_id, "
                    "source_content_sha256, style_count, slot_count, "
                    "child_job_count, candidate_count, candidate_estimate_usd, "
                    "maximum_estimated_cost_usd, cost_basis, currency, "
                    "real_generation_enabled, state, created_at, idempotency_key, "
                    "pilot_scope, product_identity_card_version_id, "
                    "product_identity_card_sha256) SELECT :new_id, customer_id, "
                    "product_id, source_asset_id, source_content_sha256, style_count, "
                    "slot_count, child_job_count, candidate_count, "
                    "candidate_estimate_usd, maximum_estimated_cost_usd, cost_basis, "
                    "currency, real_generation_enabled, state, created_at, "
                    "'suite-identity-partial', pilot_scope, "
                    "product_identity_card_version_id, NULL FROM style_suite_runs "
                    "WHERE id = :id"
                ),
                {"new_id": str(uuid4()), "id": suite_run_id},
            )

        for mismatch in ("asset_and_product", "source_digest"):
            with (
                engine.begin() as connection,
                pytest.raises(IntegrityError),
            ):
                connection.execute(
                    text(
                        "INSERT INTO product_identity_card_versions "
                        "(id, customer_id, product_asset_id, product_id, "
                        "analysis_run_id, version, source_content_sha256, card_payload, "
                        "card_sha256, confirmed_by, created_at) VALUES "
                        "(:id, :customer_id, :asset_id, :product_id, :run_id, "
                        ":version, :source_sha256, '{}', :card_sha256, "
                        "'local_customer', :now)"
                    ),
                    {
                        "id": str(uuid4()),
                        "customer_id": customer_id,
                        "asset_id": (
                            other_asset_id
                            if mismatch == "asset_and_product"
                            else asset_id
                        ),
                        "product_id": (
                            "product-b"
                            if mismatch == "asset_and_product"
                            else "product-a"
                        ),
                        "run_id": run_id,
                        "version": 2 if mismatch == "asset_and_product" else 3,
                        "source_sha256": "e" * 64,
                        "card_sha256": (
                            "d" * 64 if mismatch == "asset_and_product" else "f" * 64
                        ),
                        "now": now,
                    },
                )

        with (
            engine.begin() as connection,
            pytest.raises(
                IntegrityError,
                match="product_identity_analysis_authorization_immutable",
            ),
        ):
            connection.execute(
                text(
                    "UPDATE product_identity_analysis_runs SET model = 'other-model' "
                    "WHERE id = :id"
                ),
                {"id": run_id},
            )

        with (
            engine.begin() as connection,
            pytest.raises(IntegrityError, match="product_identity_card_immutable"),
        ):
            connection.execute(
                text(
                    "UPDATE product_identity_card_versions SET card_payload = "
                    "'{\"category\":{}}' WHERE id = :id"
                ),
                {"id": card_id},
            )

        with (
            engine.begin() as connection,
            pytest.raises(IntegrityError, match="style_suite_runs_immutable"),
        ):
            connection.execute(
                text(
                    "UPDATE style_suite_runs SET product_identity_card_sha256 = "
                    ":card_sha256 WHERE id = :id"
                ),
                {"id": suite_run_id, "card_sha256": "d" * 64},
            )
    finally:
        engine.dispose()


def test_style_suite_identity_card_columns_upgrade_nullable_for_legacy_runs(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0030_product_identity_cards")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as connection:
            triggers_before = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND lower(sql) LIKE '%style_suite_runs%'"
                    )
                )
            }
    finally:
        engine.dispose()
    command.upgrade(alembic_config, "0031_style_suite_identity_card")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("style_suite_runs")
        }
        foreign_keys = inspect(engine).get_foreign_keys("style_suite_runs")
        with engine.connect() as connection:
            triggers_after = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND lower(sql) LIKE '%style_suite_runs%'"
                    )
                )
            }
    finally:
        engine.dispose()

    assert columns["product_identity_card_version_id"]["nullable"] is True
    assert columns["product_identity_card_sha256"]["nullable"] is True
    assert any(
        foreign_key["constrained_columns"]
        == ["customer_id", "product_identity_card_version_id"]
        and foreign_key["referred_table"] == "product_identity_card_versions"
        and foreign_key["referred_columns"] == ["customer_id", "id"]
        for foreign_key in foreign_keys
    )
    assert triggers_after == triggers_before | {
        "trg_style_suite_runs_identity_card_immutable"
    }

    command.downgrade(alembic_config, "0030_product_identity_cards")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        assert "product_identity_card_version_id" not in {
            column["name"] for column in inspect(engine).get_columns("style_suite_runs")
        }
    finally:
        engine.dispose()
    command.upgrade(alembic_config, "0031_style_suite_identity_card")


def test_style_reference_pack_migration_creates_immutable_customer_scoped_tables(
    alembic_config: Config,
) -> None:
    """Dropping the pack tables, composite FKs, or triggers must break this test."""
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        tables = set(inspect(engine).get_table_names())
        pack_foreign_keys = (
            inspect(engine).get_foreign_keys("style_reference_packs")
            if "style_reference_packs" in tables
            else []
        )
        item_foreign_keys = (
            inspect(engine).get_foreign_keys("style_reference_pack_items")
            if "style_reference_pack_items" in tables
            else []
        )
        with engine.connect() as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                )
            }
    finally:
        engine.dispose()

    assert {"style_reference_packs", "style_reference_pack_items"} <= tables
    assert any(
        foreign_key["constrained_columns"]
        == ["customer_id", "scene_style_version_id"]
        and foreign_key["referred_table"] == "style_card_versions"
        for foreign_key in pack_foreign_keys
    )
    assert any(
        foreign_key["constrained_columns"] == ["customer_id", "pack_id"]
        and foreign_key["referred_table"] == "style_reference_packs"
        for foreign_key in item_foreign_keys
    )
    assert {
        "trg_style_reference_packs_immutable",
        "trg_style_reference_packs_no_delete",
        "trg_style_reference_pack_items_immutable",
        "trg_style_reference_pack_items_no_delete",
    } <= triggers


def test_style_suite_batch_scope_migration_adds_run_and_item_seals(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        run_columns = {
            column["name"] for column in inspect(engine).get_columns("style_suite_runs")
        }
        item_columns = {
            column["name"]
            for column in inspect(engine).get_columns("style_suite_run_items")
        }
    finally:
        engine.dispose()

    assert {
        "preview_payload_sha256",
        "confirmed_maximum_usd",
        "candidate_count_per_item",
        "authorization_manifest_sha256",
    } <= run_columns
    assert {
        "style_reference_pack_id",
        "style_reference_manifest_sha256",
        "item_authorized_usd",
    } <= item_columns


def test_style_suite_batch_scope_migration_replaces_legacy_cost_guard(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as connection:
            cost_trigger_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_style_suite_runs_source'"
                )
            ).scalar_one()
            snapshot_trigger_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_style_suite_items_snapshot'"
                )
            ).scalar_one()
            job_trigger_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_style_suite_items_job_update'"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert "candidate_count_per_item IS NOT NULL" in cost_trigger_sql
    assert "child_job_count * NEW.candidate_count_per_item" in cost_trigger_sql
    assert "json_tree(v.content)" in snapshot_trigger_sql
    assert "json_tree(NEW.style_snapshot)" in snapshot_trigger_sql
    assert "NEW.style_reference_manifest_sha256" in job_trigger_sql


def test_pixel_preserving_upgrade_replaces_legacy_three_candidate_count_guard(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0034_real_fidelity_suite_single_dispatch")
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        count_checks = " ".join(
            constraint["sqltext"] or ""
            for constraint in inspect(engine).get_check_constraints("style_suite_runs")
            if constraint["name"] == "ck_style_suite_runs_counts"
        )
        with engine.connect() as connection:
            preserved_triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = 'style_suite_runs'"
                    )
                )
            }
    finally:
        engine.dispose()

    assert "COALESCE(candidate_count_per_item, 3)" in count_checks
    assert {
        "trg_style_suite_runs_source",
        "trg_style_suite_runs_key_nonblank",
        "trg_style_suite_runs_key_immutable",
        "trg_style_suite_run_complete_guard",
        "trg_style_suite_runs_pilot_scope_immutable",
        "trg_style_suite_runs_identity_card_immutable",
    } <= preserved_triggers


def test_pixel_preserving_upgrade_backfills_legacy_batch_candidate_count(
    alembic_config: Config,
) -> None:
    """Populated 0034 databases must not fail the new one-candidate count guard."""
    command.upgrade(alembic_config, "0034_real_fidelity_suite_single_dispatch")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    customer_id, asset_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
    now = "2026-09-27T00:00:00+00:00"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO customers (id, display_name, created_at) "
                    "VALUES (:id, 'Legacy batch customer', :now)"
                ),
                {"id": customer_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO product_assets "
                    "(id, customer_id, product_id, photo_id, identity, confirmed, "
                    "source_type, quality_flags, occurred_at, content_sha256, mime_type, "
                    "width, height, created_at) VALUES "
                    "(:id, :customer_id, 'product-a', 'photo-a', "
                    "'original_real_photo', 1, 'upload', '[]', :now, :digest, "
                    "'image/png', 10, 10, :now)"
                ),
                {
                    "id": asset_id,
                    "customer_id": customer_id,
                    "digest": "a" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO style_suite_runs "
                    "(id, customer_id, product_id, source_asset_id, "
                    "source_content_sha256, style_count, slot_count, child_job_count, "
                    "candidate_count, candidate_estimate_usd, "
                    "maximum_estimated_cost_usd, cost_basis, currency, "
                    "real_generation_enabled, state, created_at, idempotency_key, "
                    "candidate_count_per_item) VALUES "
                    "(:id, :customer_id, 'product-a', :asset_id, :digest, "
                    "1, 1, 1, 3, 0.12, 0.12, 'local_planning_estimate', 'USD', "
                    "1, 'planned', :now, "
                    "'legacy-three-candidate-run', 1)"
                ),
                {
                    "id": run_id,
                    "customer_id": customer_id,
                    "asset_id": asset_id,
                    "digest": "a" * 64,
                    "now": now,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as connection:
            per_item = connection.execute(
                text(
                    "SELECT candidate_count_per_item FROM style_suite_runs "
                    "WHERE id = :run_id"
                ),
                {"run_id": run_id},
            ).scalar_one()
    finally:
        engine.dispose()

    assert per_item == 3


def test_mask_revision_migration_preserves_existing_foreground_versions(
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "0035_pixel_preserving_isolated_style")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    customer_id, asset_id, foreground_id = str(uuid4()), str(uuid4()), str(uuid4())
    now = "2026-09-27T00:00:00+00:00"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO customers (id, display_name, created_at) "
                    "VALUES (:id, 'Mask upgrade customer', :now)"
                ),
                {"id": customer_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO product_assets "
                    "(id, customer_id, product_id, photo_id, identity, confirmed, "
                    "source_type, quality_flags, occurred_at, content_sha256, mime_type, "
                    "width, height, created_at) VALUES "
                    "(:id, :customer_id, 'product-a', 'photo-a', "
                    "'original_real_photo', 1, 'upload', '[]', :now, :digest, "
                    "'image/png', 10, 10, :now)"
                ),
                {
                    "id": asset_id,
                    "customer_id": customer_id,
                    "digest": "a" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO product_foreground_versions "
                    "(id, customer_id, product_asset_id, product_id, version, "
                    "source_content_sha256, foreground_sha256, mask_sha256, width, "
                    "height, extractor_version, manifest_sha256, state, quality_flags, "
                    "created_at, updated_at) VALUES "
                    "(:id, :customer_id, :asset_id, 'product-a', 1, :source, "
                    ":foreground, :mask, 10, 10, 'fake-v1', :manifest, 'proposed', "
                    "'[]', :now, :now)"
                ),
                {
                    "id": foreground_id,
                    "customer_id": customer_id,
                    "asset_id": asset_id,
                    "source": "a" * 64,
                    "foreground": "b" * 64,
                    "mask": "c" * 64,
                    "manifest": "d" * 64,
                    "now": now,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT source_content_sha256, mask_sha256, state "
                    "FROM product_foreground_versions WHERE id = :id"
                ),
                {"id": foreground_id},
            ).one()
            tables = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()

    assert tuple(row) == ("a" * 64, "c" * 64, "proposed")
    assert "product_foreground_mask_revisions" in tables
