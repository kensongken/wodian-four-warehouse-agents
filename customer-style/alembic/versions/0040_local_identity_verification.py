"""Allow zero-cost local verification for an identity card's exact source image."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_local_identity_verification"
down_revision: str | None = "0039_product_identity_observation_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def _replace_constraints(*, local_enabled: bool) -> None:
    table = "product_identity_observation_runs"
    if op.get_bind().dialect.name == "sqlite":
        connection = op.get_bind()
        existing_fk_violations = {
            tuple(row)
            for row in connection.exec_driver_sql(
                "PRAGMA foreign_key_check"
            ).fetchall()
        }
        triggers = _sqlite_triggers_referencing(table)
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                for name, _ in triggers:
                    op.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                with op.batch_alter_table(table, recreate="always") as batch:
                    if local_enabled:
                        batch.drop_constraint(
                            "ck_product_identity_observation_usd_positive",
                            type_="check",
                        )
                        batch.drop_constraint(
                            "ck_product_identity_observation_dispatch_state",
                            type_="check",
                        )
                        batch.create_check_constraint(
                            "ck_product_identity_observation_usd_nonnegative",
                            "authorized_usd >= 0",
                        )
                        batch.create_check_constraint(
                            "ck_product_identity_observation_dispatch_state",
                            "dispatch_state IN ('reserved', 'dispatch_uncertain', "
                            "'provider_responded', 'locally_verified')",
                        )
                    else:
                        batch.drop_constraint(
                            "ck_product_identity_observation_usd_nonnegative",
                            type_="check",
                        )
                        batch.drop_constraint(
                            "ck_product_identity_observation_dispatch_state",
                            type_="check",
                        )
                        batch.create_check_constraint(
                            "ck_product_identity_observation_usd_positive",
                            "authorized_usd > 0",
                        )
                        batch.create_check_constraint(
                            "ck_product_identity_observation_dispatch_state",
                            "dispatch_state IN ('reserved', 'dispatch_uncertain', "
                            "'provider_responded')",
                        )
                for _, statement in triggers:
                    op.execute(statement)
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            remaining_fk_violations = {
                tuple(row)
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            }
            if remaining_fk_violations != existing_fk_violations:
                raise RuntimeError("local identity verification migration foreign key check failed")
        return

    if local_enabled:
        op.drop_constraint(
            "ck_product_identity_observation_usd_positive", table, type_="check"
        )
        op.drop_constraint(
            "ck_product_identity_observation_dispatch_state", table, type_="check"
        )
        op.create_check_constraint(
            "ck_product_identity_observation_usd_nonnegative",
            table,
            "authorized_usd >= 0",
        )
        op.create_check_constraint(
            "ck_product_identity_observation_dispatch_state",
            table,
            "dispatch_state IN ('reserved', 'dispatch_uncertain', "
            "'provider_responded', 'locally_verified')",
        )
    else:
        op.drop_constraint(
            "ck_product_identity_observation_usd_nonnegative", table, type_="check"
        )
        op.drop_constraint(
            "ck_product_identity_observation_dispatch_state", table, type_="check"
        )
        op.create_check_constraint(
            "ck_product_identity_observation_usd_positive",
            table,
            "authorized_usd > 0",
        )
        op.create_check_constraint(
            "ck_product_identity_observation_dispatch_state",
            table,
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
        )


def upgrade() -> None:
    _replace_constraints(local_enabled=True)


def downgrade() -> None:
    incompatible = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM product_identity_observation_runs "
            "WHERE authorized_usd = 0 OR dispatch_state = 'locally_verified'"
        )
    ).scalar_one()
    if incompatible:
        raise RuntimeError("cannot discard sealed local identity verification evidence")
    _replace_constraints(local_enabled=False)
