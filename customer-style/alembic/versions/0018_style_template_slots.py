"""Persist immutable suite slots pinned to customer-owned scene revisions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_style_template_slots"
down_revision: str | None = "0017_style_sample_consent_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "style_template_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False
        ),
        sa.Column("scene_style_id", sa.String(36), nullable=False),
        sa.Column("style_card_version_id", sa.String(36), nullable=False),
        sa.Column("slot_key", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "scene_style_id"],
            ["scene_styles.customer_id", "scene_styles.id"],
            name="fk_style_template_slots_customer_scene",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_template_slots_customer_version",
        ),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_style_template_slots_customer_id"
        ),
        sa.UniqueConstraint(
            "style_card_version_id",
            "slot_key",
            name="uq_style_template_slots_version_key",
        ),
        sa.UniqueConstraint(
            "style_card_version_id",
            "position",
            name="uq_style_template_slots_version_position",
        ),
        sa.CheckConstraint("position >= 0", name="ck_style_template_slots_position"),
    )
    for column in ("customer_id", "scene_style_id", "style_card_version_id"):
        op.create_index(
            f"ix_style_template_slots_{column}", "style_template_slots", [column]
        )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_style_template_slots_no_{action.lower()} "
            f"BEFORE {action} ON style_template_slots "
            "BEGIN SELECT RAISE(ABORT, 'style_template_slot_immutable'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_style_template_slots_scene_version "
        "BEFORE INSERT ON style_template_slots WHEN NOT EXISTS ("
        "SELECT 1 FROM scene_styles s JOIN style_card_versions v "
        "ON v.style_card_id = s.style_card_id "
        "WHERE s.id = NEW.scene_style_id AND s.customer_id = NEW.customer_id "
        "AND v.id = NEW.style_card_version_id AND v.customer_id = NEW.customer_id) "
        "BEGIN SELECT RAISE(ABORT, 'style_template_slot_version_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_template_slots_snapshot "
        "BEFORE INSERT ON style_template_slots WHEN NOT EXISTS ("
        "SELECT 1 FROM style_card_versions v, json_each(v.content, '$.slots') slot "
        "WHERE v.id = NEW.style_card_version_id AND v.customer_id = NEW.customer_id "
        "AND json_extract(slot.value, '$.id') = NEW.id "
        "AND json_extract(slot.value, '$.customer_id') = NEW.customer_id "
        "AND json_extract(slot.value, '$.scene_style_id') = NEW.scene_style_id "
        "AND json_extract(slot.value, '$.style_card_version_id') = NEW.style_card_version_id "
        "AND json_extract(slot.value, '$.slot_key') = NEW.slot_key "
        "AND json_extract(slot.value, '$.label') = NEW.label "
        "AND json_extract(slot.value, '$.position') = NEW.position "
        "AND json_extract(slot.value, '$.active') = NEW.active "
        "AND json_extract(slot.value, '$.rules') = json(NEW.rules)) "
        "BEGIN SELECT RAISE(ABORT, 'style_template_slot_snapshot_mismatch'); END"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM style_template_slots"))
        .scalar_one()
    ):
        raise RuntimeError("cannot discard confirmed style template slots")
    op.drop_table("style_template_slots")
