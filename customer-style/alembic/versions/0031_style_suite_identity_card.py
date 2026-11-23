"""Seal a confirmed product identity card into style suite runs."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_style_suite_identity_card"
down_revision: str | None = "0030_product_identity_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TRIGGER = "trg_style_suite_runs_identity_card_immutable"


def _sqlite_triggers_referencing(table_name: str) -> list[tuple[str, str]]:
    if op.get_bind().dialect.name != "sqlite":
        return []
    rows = op.get_bind().execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND lower(sql) LIKE :table_name AND sql IS NOT NULL"
        ),
        {"table_name": f"%{table_name.lower()}%"},
    )
    return [(str(row[0]), str(row[1])) for row in rows]


def _drop_triggers(triggers: list[tuple[str, str]]) -> None:
    for name, _ in triggers:
        op.execute(f'DROP TRIGGER IF EXISTS "{name}"')


def _restore_triggers(triggers: list[tuple[str, str]]) -> None:
    for _, statement in triggers:
        op.execute(statement)


def _create_identity_card_immutable_trigger() -> None:
    op.execute(
        f"CREATE TRIGGER {_IMMUTABLE_TRIGGER} "
        "BEFORE UPDATE OF product_identity_card_version_id, "
        "product_identity_card_sha256 ON style_suite_runs WHEN "
        "NEW.product_identity_card_version_id IS NOT "
        "OLD.product_identity_card_version_id OR "
        "NEW.product_identity_card_sha256 IS NOT "
        "OLD.product_identity_card_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_runs_immutable'); END"
    )


def upgrade() -> None:
    triggers = _sqlite_triggers_referencing("style_suite_runs")
    if op.get_bind().dialect.name == "sqlite":
        connection = op.get_bind()
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                _drop_triggers(triggers)
                with op.batch_alter_table(
                    "style_suite_runs", recreate="always"
                ) as batch:
                    batch.add_column(
                        sa.Column(
                            "product_identity_card_version_id",
                            sa.String(36),
                            nullable=True,
                        )
                    )
                    batch.add_column(
                        sa.Column(
                            "product_identity_card_sha256",
                            sa.String(64),
                            nullable=True,
                        )
                    )
                    batch.create_foreign_key(
                        "fk_style_suite_runs_customer_identity_card",
                        "product_identity_card_versions",
                        ["customer_id", "product_identity_card_version_id"],
                        ["customer_id", "id"],
                    )
                    batch.create_check_constraint(
                        "ck_style_suite_runs_identity_card_complete",
                        "(product_identity_card_version_id IS NULL AND "
                        "product_identity_card_sha256 IS NULL) OR "
                        "(product_identity_card_version_id IS NOT NULL AND "
                        "product_identity_card_sha256 IS NOT NULL AND "
                        "length(product_identity_card_sha256) = 64)",
                    )
                _restore_triggers(triggers)
                _create_identity_card_immutable_trigger()
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError(
                    "style suite identity migration foreign key check failed"
                )
        return
    op.add_column(
        "style_suite_runs",
        sa.Column("product_identity_card_version_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "style_suite_runs",
        sa.Column("product_identity_card_sha256", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_style_suite_runs_customer_identity_card",
        "style_suite_runs",
        "product_identity_card_versions",
        ["customer_id", "product_identity_card_version_id"],
        ["customer_id", "id"],
    )
    op.create_check_constraint(
        "ck_style_suite_runs_identity_card_complete",
        "style_suite_runs",
        "(product_identity_card_version_id IS NULL AND "
        "product_identity_card_sha256 IS NULL) OR "
        "(product_identity_card_version_id IS NOT NULL AND "
        "product_identity_card_sha256 IS NOT NULL AND "
        "length(product_identity_card_sha256) = 64)",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM style_suite_runs "
                "WHERE product_identity_card_version_id IS NOT NULL "
                "OR product_identity_card_sha256 IS NOT NULL"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("cannot discard sealed style suite identity cards")
    if op.get_bind().dialect.name == "sqlite":
        connection = op.get_bind()
        existing_fk_violations = {
            tuple(row)
            for row in connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        }
        op.execute(f"DROP TRIGGER IF EXISTS {_IMMUTABLE_TRIGGER}")
        triggers = _sqlite_triggers_referencing("style_suite_runs")
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                _drop_triggers(triggers)
                with op.batch_alter_table(
                    "style_suite_runs", recreate="always"
                ) as batch:
                    batch.drop_constraint(
                        "ck_style_suite_runs_identity_card_complete", type_="check"
                    )
                    batch.drop_constraint(
                        "fk_style_suite_runs_customer_identity_card",
                        type_="foreignkey",
                    )
                    batch.drop_column("product_identity_card_sha256")
                    batch.drop_column("product_identity_card_version_id")
                _restore_triggers(triggers)
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            remaining_fk_violations = {
                tuple(row)
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            }
            if remaining_fk_violations != existing_fk_violations:
                raise RuntimeError(
                    "style suite identity downgrade foreign key check failed"
                )
        return
    op.drop_constraint(
        "ck_style_suite_runs_identity_card_complete",
        "style_suite_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_style_suite_runs_customer_identity_card",
        "style_suite_runs",
        type_="foreignkey",
    )
    op.drop_column("style_suite_runs", "product_identity_card_sha256")
    op.drop_column("style_suite_runs", "product_identity_card_version_id")
