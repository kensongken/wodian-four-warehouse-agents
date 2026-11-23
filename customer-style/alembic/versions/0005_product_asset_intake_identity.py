"""Make product-asset intake identities atomic and permission ordering stable."""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_product_asset_intake_identity"
down_revision: str | None = "0004_product_image_output"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enforce one content identity per customer photo and permission timestamp."""
    op.create_index(
        "uq_product_assets_customer_photo_identity",
        "product_assets",
        ["customer_id", "photo_id"],
        unique=True,
    )
    op.create_index(
        "uq_product_asset_permission_events_asset_time",
        "product_asset_permission_events",
        ["customer_id", "product_asset_id", "occurred_at"],
        unique=True,
    )


def downgrade() -> None:
    """Remove intake-specific uniqueness indexes."""
    op.drop_index(
        "uq_product_asset_permission_events_asset_time",
        table_name="product_asset_permission_events",
    )
    op.drop_index(
        "uq_product_assets_customer_photo_identity", table_name="product_assets"
    )
