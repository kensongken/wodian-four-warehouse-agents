"""Scope active scene names by their persisted evidence origin."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_scene_style_origin_scope"
down_revision: str | None = "0023_real_pilot_authorization_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scene_styles",
        sa.Column(
            "style_origin",
            sa.String(length=32),
            nullable=False,
            server_default="customer_upload",
        ),
    )
    bind = op.get_bind()
    conflicting = bind.execute(
        sa.text(
            "SELECT s.id FROM scene_styles s "
            "WHERE EXISTS (SELECT 1 FROM style_card_versions v "
            "WHERE v.customer_id = s.customer_id "
            "AND v.style_card_id = s.style_card_id "
            "AND json_extract(v.content, '$.style_origin') = 'local_demo') "
            "AND EXISTS (SELECT 1 FROM style_card_versions v "
            "WHERE v.customer_id = s.customer_id "
            "AND v.style_card_id = s.style_card_id "
            "AND json_extract(v.content, '$.style_origin') = 'customer_upload') "
            "LIMIT 1"
        )
    ).first()
    if conflicting is not None:
        raise RuntimeError("scene style history has conflicting origins")
    bind.execute(
        sa.text(
            "UPDATE scene_styles SET style_origin = 'local_demo' "
            "WHERE EXISTS (SELECT 1 FROM style_card_versions v "
            "WHERE v.customer_id = scene_styles.customer_id "
            "AND v.style_card_id = scene_styles.style_card_id "
            "AND json_extract(v.content, '$.style_origin') = 'local_demo')"
        )
    )
    op.drop_index("uq_scene_styles_active_customer_name", table_name="scene_styles")
    op.create_index(
        "uq_scene_styles_active_customer_origin_name",
        "scene_styles",
        ["customer_id", "style_origin", "name"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )
    op.execute(
        "CREATE TRIGGER trg_scene_styles_origin_valid "
        "BEFORE INSERT ON scene_styles "
        "WHEN NEW.style_origin NOT IN ('customer_upload', 'local_demo') "
        "BEGIN SELECT RAISE(ABORT, 'scene_style_origin_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_scene_styles_origin_immutable "
        "BEFORE UPDATE OF style_origin ON scene_styles "
        "WHEN NEW.style_origin IS NOT OLD.style_origin "
        "BEGIN SELECT RAISE(ABORT, 'scene_style_origin_immutable'); END"
    )


def downgrade() -> None:
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT customer_id, name FROM scene_styles "
                "WHERE active = 1 GROUP BY customer_id, name "
                "HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError("cannot collapse active scene names across origins")
    op.execute("DROP TRIGGER IF EXISTS trg_scene_styles_origin_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_scene_styles_origin_valid")
    op.drop_index(
        "uq_scene_styles_active_customer_origin_name", table_name="scene_styles"
    )
    op.create_index(
        "uq_scene_styles_active_customer_name",
        "scene_styles",
        ["customer_id", "name"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )
    op.drop_column("scene_styles", "style_origin")
