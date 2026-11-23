"""add ProductImageSetReady publication receipts

Revision ID: 0019_image_set_publication
Revises: 0018_image_set_delivery
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_image_set_publication"
down_revision = "0018_image_set_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_image_set_deliveries",
        sa.Column(
            "publication_status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "product_image_set_deliveries",
        sa.Column(
            "publication_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "product_image_set_deliveries",
        sa.Column(
            "publication_next_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "product_image_set_deliveries",
        sa.Column("publication_last_error_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "product_image_set_deliveries",
        sa.Column(
            "publication_delivered_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("product_image_set_deliveries", "publication_delivered_at")
    op.drop_column("product_image_set_deliveries", "publication_last_error_code")
    op.drop_column("product_image_set_deliveries", "publication_next_attempt_at")
    op.drop_column("product_image_set_deliveries", "publication_attempts")
    op.drop_column("product_image_set_deliveries", "publication_status")
